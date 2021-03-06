# -*- coding: utf-8 -*-
#
# Author: Eric Kow
# License: BSD3

"""
STAC-specific conventions related to graphs.
"""

import copy
import collections
import itertools
import textwrap

from educe import corpus, stac, annotation
from educe.graph import *
import educe.graph
from pygraph.readwrite import dot
import pydot
import pygraph.classes.hypergraph as gr
import pygraph.classes.digraph    as dgr
from pygraph.algorithms import traversal
from pygraph.algorithms import accessibility

class MultiheadedCduException(Exception):
    def __init__(self, cdu, *args, **kw):
        self.cdu = cdu
        Exception.__init__(self, *args, **kw)

class Graph(educe.graph.Graph):
    def __init__(self):
        return educe.graph.Graph.__init__(self)

    @classmethod
    def from_doc(cls, corpus, doc_key):
        return super(Graph, cls).from_doc(corpus, doc_key)

    def is_cdu(self, x):
        return super(Graph, self).is_cdu(x) and\
                stac.is_cdu(self.annotation(x))

    def is_edu(self, x):
        return super(Graph, self).is_edu(x) and\
                stac.is_edu(self.annotation(x))

    def is_relation(self, x):
        return super(Graph, self).is_relation(x) and\
                stac.is_relation_instance(self.annotation(x))

    # --------------------------------------------------
    # recursive head for CDU
    # --------------------------------------------------

    def cdu_head(self, cdu, sloppy=False):
        """
        Given a CDU, return its head, defined here as the only DU
        that is not pointed to by any other member of this CDU.

        This is meant to approximate the description in Muller 2012
        (/Constrained decoding for text-level discourse parsing/):

        1. in the highest DU in its subgraph in terms of suboordinate
           relations
        2. in case of a tie in #1, the leftmost in terms of coordinate
           relations

        Corner cases:

        * Return None if the CDU has no members (annotation error)
        * If the CDU contains more than one head (annotation error)
          and if sloppy is True, return the textually leftmost one;
          otherwise, raise a MultiheadedCduException
        """
        if self.has_node(cdu):
            hyperedge = self.mirror(cdu)
        else:
            hyperedge = cdu

        members    = self.cdu_members(cdu)
        candidates = []
        for m in members:
            def points_to_me(l): # some other member of this CDU
                                 # points to me via this link
                return l != hyperedge\
                        and self.is_relation(l)\
                        and self.links(l)[1] == m\
                        and self.links(l)[0] in members
            pointed_to = filter(points_to_me, self.links(m))
            if not (self.is_relation(m) or pointed_to):
                candidates.append(m)

        if len(candidates) == 0:
            return None
        elif len(candidates) == 1 or sloppy:
            c = self.sorted_first_widest(candidates)[0]
            if self.is_cdu(c):
                return self.mirror(c)
            else:
                return c
        else:
            raise MultiheadedCduException(cdu)

    def recursive_cdu_heads(self, sloppy=False):
        """
        A dictionary mapping each CDU to its recursive CDU
        head (see `cdu_head`)
        """
        cache = {}
        def get_head(c):
            if c in cache:
                return cache[c]
            else:
                hd = self.cdu_head(c, sloppy)
                if hd is None: return None
                if self.is_cdu(hd):
                    deep_hd = get_head(hd)
                else:
                    deep_hd = hd
                if deep_hd is None:
                    return None
                else:
                    cache[c] = deep_hd
                    return deep_hd
        for c in self.cdus():
            get_head(c)
        return cache

    def without_cdus(self, sloppy=False):
        """
        Return a deep copy of this graph with all CDUs removed.
        Links involving these CDUs will point instead from/to
        their deep heads
        """
        g2    = copy.deepcopy(self)
        heads = g2.recursive_cdu_heads(sloppy)
        anno_heads = dict((g2.annotation(k),g2.annotation(v))\
                          for k,v in heads.items())
        # replace all links to/from cdus with to/from their heads
        for e_edge in g2.relations():
            links  = g2.links(e_edge)
            attrs  = g2.edge_attributes(e_edge)
            if any(g2.is_cdu(l) for l in links):
                # recreate the edge
                g2.del_edge(e_edge)
                g2.add_edge(e_edge)
                g2.add_edge_attributes(e_edge, attrs)
                for l in links:
                    l2 = heads[g2.mirror(l)] if g2.is_cdu(l) else l
                    g2.link(l2, e_edge)
        # now that we've pointed everything away, nuke the CDUs
        for e_cdu in g2.cdus():
            g2.del_node(g2.mirror(e_cdu))
            g2.del_edge(e_cdu)
        # to be on the safe side, we should also do similar link-rewriting
        # but on the underlying educe.annotation objects layer
        # (symptom of a yucky design) :-(
        for r in g2.doc.relations:
            if stac.is_relation_instance(r):
                src  = r.source
                tgt  = r.target
                src2 = anno_heads.get(src, src)
                tgt2 = anno_heads.get(tgt, tgt)
                r.source = src2
                r.target = tgt2
                r.span   = annotation.RelSpan(src2.local_id(), tgt2.local_id())
        # remove the actual CDU objects too
        g2.doc.schemas = [ s for s in g2.doc.schemas if not stac.is_cdu(s) ]
        return g2

    # --------------------------------------------------
    # right frontier constraint
    # --------------------------------------------------

    def sorted_first_widest(self, xs):
        """
        Given a list of nodes, return the nodes ordered by their starting point,
        and in case of a tie their inverse width (ie. widest first).
        """
        def span(n):
            return self.annotation(n).text_span()

        def from_span(sp):
            # negate the endpoint so that if we have a tie on the starting
            # point, the widest span comes first
            return (sp.char_start, 0 - sp.char_end)

        tagged = sorted((from_span(span(x)),x) for x in xs)
        return [x for _,x in tagged]

    def first_widest_dus(self):
        """
        Return discourse units in this graph, ordered by their starting point,
        and in case of a tie their inverse width (ie. widest first)
        """
        def is_interesting_du(n):
            return self.is_edu(n) or\
                (self.is_cdu(n) and self.cdu_members(n))

        dus = filter(is_interesting_du,self.nodes())
        return self.sorted_first_widest(dus)


    def _build_right_frontier(self, points, last):
        """
        Given a dictionary mapping each node to its closest
        right frontier node, generate a path up that frontier.
        """
        frontier = []
        current  = last
        while current in points:
            next    = points[current]
            yield current
            current = next

    def _is_on_right_frontier(self, points, last, node):
        """
        Return True if node is on the right frontier as
        represented by the pair points/last.

        This uses `build_frontier`
        """
        return any(fnode == node for fnode in
                   self._build_right_frontier(points, last))

    def _frontier_points(self, nodes):
        """
        Given an ordered sequence of nodes in this graph return a dictionary
        mapping each node to the nearest node (in the sequence) that either

        * points to it with a subordinating relation
        * includes it as a CDU member
        """
        points = {}
        def position(n):
            if n in nodes:
                return nodes.index(n)
            else:
                return -1

        for n1 in nodes:
            candidates = []

            def is_incoming_subordinate_rel(l):
                ns = self.links(l)
                return self.is_relation(l)\
                        and stac.is_subordinating(self.annotation(l))\
                        and len(ns) == 2 and ns[1] == n1

            def add_candidate(n2):
                candidates.append((n2,position(n2)))

            for l in self.links(n1):
                if is_incoming_subordinate_rel(l):
                    n2 = self.links(l)[0]
                    add_candidate(n2)
                elif self.is_cdu(l):
                    n2 = self.mirror(l)
                    add_candidate(n2)

            if candidates:
                best = max(candidates, key=lambda x:x[1])
                points[n1] = best[0]
            else:
                points[n1] = None

        return points

    def right_frontier_violations(self):
        nodes      = self.first_widest_dus()
        violations = collections.defaultdict(list)
        if len(nodes) < 2:
            return violations

        points = self._frontier_points(nodes)
        nexts  = itertools.islice(nodes, 1, None)
        for last,n1 in itertools.izip(nodes, nexts):
            def is_incoming(l):
                ns = self.links(l)
                return self.is_relation(l) and len(ns) == 2 and ns[1] == n1

            for l in self.links(n1):
                if not is_incoming(l): continue
                n2 = self.links(l)[0]
                if not self._is_on_right_frontier(points, last, n2):
                    violations[n2].append(l)
        return violations

class DotGraph(educe.graph.DotGraph):
    """
    A dot representation of this graph for visualisation.
    The `to_string()` method is most likely to be of interest here
    """

    def __init__(self, anno_graph):
        doc   = anno_graph.doc
        nodes = anno_graph.first_widest_dus()
        self.node_order = {}
        for i,n in enumerate(nodes):
            self.node_order[anno_graph.annotation(n)] = i
        educe.graph.DotGraph.__init__(self, anno_graph)

    def _get_turn_info(self, u):
        enclosing_turns = [ t for t in self.turns if t.span.encloses(u.span) ]
        if len(enclosing_turns) > 0:
            turn      = enclosing_turns[0]
            speaker   = turn.features['Emitter']
            turn_text = stac.split_turn_text(self.doc.text(turn.span))[0]
            turn_id   = turn_text.split(':')[0].strip()
            return speaker, turn_id
        else:
            return None, None

    def _get_speech_acts(self, anno):
        # In discourse annotated part of the corpus, all segments have
        # type 'Other', which isn't too helpful. Try to recover the
        # speech act from the unit equivalent to this document
        twin = stac.twin(self.corpus, anno)
        edu  = twin if twin is not None else anno
        return stac.dialogue_act(edu)

    def _get_addressee(self, anno):
        # In discourse annotated part of the corpus, all segments have
        # type 'Other', which isn't too helpful. Try to recover the
        # speech act from the unit equivalent to this document
        twin = stac.twin(self.corpus, anno)
        edu  = twin if twin is not None else anno
        return edu.features.get('Addressee', None)

    def _edu_label(self, anno):
        speech_acts  = ", ".join(self._get_speech_acts(anno))
        speaker, tid = self._get_turn_info(anno)
        addressee    = self._get_addressee(anno)

        if speaker is None:
            speaker_prefix = '(%s)'  % tid
        elif addressee is None:
            speaker_prefix = '(%s: %s) ' % (tid, speaker)
        else:
            speaker_prefix = '(%s: %s to %s) ' % (tid, speaker, addressee)

        if callable(getattr(anno, "text_span", None)):
            span = ' ' + str(anno.text_span())
        else:
            span = ''
        text     = self.doc.text(anno.span)
        return "%s%s [%s]%s" % (speaker_prefix, text, speech_acts, span)

    def _add_edu(self, node):
        anno  = self.core.annotation(node)
        label = self._edu_label(anno)
        attrs = { 'label' : textwrap.fill(label, 30)
                , 'shape' : 'plaintext'
                }
        if not self._edu_label(anno) or not stac.is_edu(anno):
            attrs['fontcolor'] = 'red'
        self.add_node(pydot.Node(node, **attrs))

    def _rel_label(self, anno):
        return anno.type

    def _simple_rel_attrs(self, anno):
        attrs = educe.graph.DotGraph._simple_rel_attrs(self, anno)
        if anno.type not in stac.subordinating_relations:
            attrs['fontcolor'] = 'dodgerblue4'
            attrs['color'    ] = 'gray13'
        return attrs

    def _complex_rel_attrs(self, anno):
        midpoint_attrs, attrs1, attrs2 =\
                educe.graph.DotGraph._complex_rel_attrs(self, anno)
        if anno.type not in stac.subordinating_relations:
            midpoint_attrs['fontcolor'] = 'dodgerblue4'
        return (midpoint_attrs, attrs1, attrs2)

    def _simple_cdu_attrs(self, anno):
        attrs = educe.graph.DotGraph._simple_cdu_attrs(self, anno)
        attrs['rank'] = 'same'
        if anno in self.node_order:
            attrs['label'] = '%d. CDU' % self.node_order[anno]
        return attrs

