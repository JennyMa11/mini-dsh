"""Routing, graph and relational fixtures; split metadata is authoritative."""

# ruff: noqa: E501 - literal fixture code keeps meaningful source lines

from minidsh_rl.learning_contracts import FamilyDefinition, Mutation


def router() -> FamilyDefinition:
    boundary = Mutation(
        "paths.py",
        "path.startswith(prefix + '/')",
        "path.startswith(prefix)",
        "path segment boundary",
    )
    root = Mutation("paths.py", "prefix == '/' or", "False or", "root matches every absolute path")
    priority = Mutation(
        "selection.py",
        "int(method != '*')",
        "int(method == '*')",
        "exact method beats wildcard at equal prefix length",
    )
    return FamilyDefinition(
        "path_router",
        "train",
        "Select a route from routes=[method,prefix,target], method and path. Paths begin with /. "
        "Ignore trailing /; a prefix matches complete path segments, and / matches every path. "
        "Methods are case-insensitive; * matches any method. Prefer longest prefix, then exact method, "
        "then earliest route declaration. Return target or None. Never mutate input.",
        (
            (
                "paths.py",
                """
                def canonical(path):
                    return '/' + path.strip('/')

                def path_matches(path, prefix):
                    return prefix == '/' or path == prefix or path.startswith(prefix + '/')
            """,
            ),
            (
                "matching.py",
                """
                def method_matches(method, route_method):
                    return route_method == '*' or route_method.upper() == method.upper()
            """,
            ),
            (
                "selection.py",
                """
                def route_score(prefix, method, order):
                    return len(prefix), int(method != '*'), -order

                def choose(candidates):
                    if not candidates:
                        return None
                    return max(candidates, key=lambda item: item[0])[1]
            """,
            ),
            (
                "routing.py",
                """
                from paths import canonical, path_matches
                from matching import method_matches
                from selection import route_score, choose

                def route(routes, method, path):
                    candidates = []
                    for order, row in enumerate(routes):
                        route_method, raw_prefix, target = row
                        prefix = canonical(raw_prefix)
                        if method_matches(method, route_method) and path_matches(canonical(path), prefix):
                            candidates.append((route_score(prefix, route_method, order), target))
                    return choose(candidates)
            """,
            ),
        ),
        "from routing import route",
        "route(payload['routes'], payload['method'], payload['path'])",
        ("routes", "method", "path"),
        ((boundary, priority), (root, priority)),
        (
            Mutation(
                "matching.py",
                "route_method.upper() == method.upper()",
                "route_method == method",
                "method case folding",
            ),
            Mutation("selection.py", "-order", "order", "earliest declaration wins ties"),
        ),
        """
        def request(routes, path, method='GET'):
            return solve({'routes': routes, 'path': path, 'method': method})
        assert request([['*','/','root']], '/child') == 'root', 'contract: root'
        assert request([['GET','/a','bad']], '/ab') is None, 'contract: segment'
        assert request([['*','/a','wild'], ['get','/a','exact']], '/a/b') == 'exact', 'contract: exact method'
        assert request([['get','/a','one']], '/a', 'GET') == 'one', 'contract: method case'
        assert request([['GET','/a','first'], ['GET','/a','last']], '/a') == 'first', 'contract: declaration order'
        assert request([['GET','/a','short'], ['GET','/a/b','long']], '/a/b/c') == 'long', 'contract: longest'
        """,
        """
        p = {'routes': [['POST','/x/','outer'], ['*','/x/y','inner']], 'method':'post', 'path':'/x/y/z/'}
        assert solve(p) == 'inner'
        assert p['routes'][0] == ['POST','/x/','outer']
        assert solve({'routes': [['GET','/k','no']], 'method':'POST', 'path':'/k'}) is None
        assert solve({'routes': [], 'method':'GET', 'path':'/'}) is None
        assert solve({'routes': [['*','/','base'], ['GET','/z','z']], 'method':'put', 'path':'/other'}) == 'base'
        assert solve({'routes': [['GET','/r','wrong']], 'method':'GET', 'path':'/rubbish'}) is None
        assert solve({'routes': [['*','/t','broad'], ['POST','/t','precise']], 'method':'post', 'path':'/t'}) == 'precise'
        assert solve({'routes': [['PUT','/tie','earlier'], ['PUT','/tie','later']], 'method':'PUT', 'path':'/tie'}) == 'earlier'
        """,
    )


def dependencies() -> FamilyDefinition:
    orientation = Mutation(
        "graph.py",
        "graph[child].add(parent)",
        "graph[parent].add(child)",
        "edge parent precedes child",
    )
    self_edge = Mutation(
        "graph.py",
        "graph[child].add(parent)",
        "graph[child].add(child)",
        "dependency is parent, not self",
    )
    batch = Mutation(
        "scheduler.py",
        "done.update(layer)",
        "done.add(layer[0])",
        "whole batch commits before next batch",
    )
    return FamilyDefinition(
        "dependency_batches",
        "train",
        "nodes are unique strings; edges=[parent,child] with endpoints in nodes. Return sorted ready-node "
        "batches: dependencies must have finished in a previous batch. Duplicate edges have no extra effect. "
        "Empty graph returns []; cycles raise ValueError. Do not mutate input. Preserve APIs.",
        (
            (
                "graph.py",
                """
                def dependency_map(nodes, edges):
                    graph = {node: set() for node in nodes}
                    for parent, child in edges:
                        graph[child].add(parent)
                    return graph
            """,
            ),
            (
                "ready.py",
                """
                def ready_nodes(graph, done):
                    return sorted([node for node in graph if node not in done and graph[node] <= done])
            """,
            ),
            (
                "scheduler.py",
                """
                from ready import ready_nodes

                def run_batches(graph):
                    done = set()
                    result = []
                    while len(done) < len(graph):
                        layer = ready_nodes(graph, done)
                        if not layer:
                            raise ValueError('cycle')
                        result.append(layer)
                        done.update(layer)
                    return result
            """,
            ),
        ),
        "from graph import dependency_map\nfrom scheduler import run_batches",
        "run_batches(dependency_map(payload['nodes'], payload['edges']))",
        ("nodes", "edges"),
        ((orientation, batch), (self_edge, batch)),
        (
            Mutation(
                "ready.py",
                "return sorted([node for node in graph if node not in done and graph[node] <= done])",
                "return list([node for node in graph if node not in done and graph[node] <= done])",
                "ready batch sorted by ID",
            ),
            Mutation(
                "scheduler.py",
                "raise ValueError('cycle')",
                "return result",
                "cycle is an error, not partial success",
            ),
        ),
        """
        assert solve({'nodes':['b','a','c'], 'edges':[['a','c'],['a','c']]}) == [['a','b'],['c']], 'contract: batch/sort/duplicates'
        assert solve({'nodes':['x','y'], 'edges':[['x','y']]}) == [['x'],['y']], 'contract: edge direction'
        assert solve({'nodes':[], 'edges':[]}) == [], 'contract: empty'
        try:
            solve({'nodes':['p','q'], 'edges':[['p','q'],['q','p']]})
        except ValueError:
            pass
        else:
            raise AssertionError('contract: cycle')
        """,
        """
        p = {'nodes':['d','c','b','a'], 'edges':[['a','b'],['a','c'],['b','d'],['c','d']]}
        assert solve(p) == [['a'],['b','c'],['d']]
        assert p['nodes'] == ['d','c','b','a']
        assert solve({'nodes':['z'], 'edges':[]}) == [['z']]
        try:
            solve({'nodes':['z'], 'edges':[['z','z']]})
        except ValueError:
            pass
        else:
            raise AssertionError('self cycle')
        """,
    )


def table_join() -> FamilyDefinition:
    duplicates = Mutation(
        "indexing.py",
        "index.setdefault(key, []).append(value)",
        "index[key] = [value]",
        "right duplicates all retained",
    )
    order = Mutation(
        "indexing.py",
        "index.setdefault(key, []).append(value)",
        "index.setdefault(key, []).insert(0, value)",
        "right match order preserved",
    )
    mode = Mutation(
        "joining.py",
        "if not matches and mode == 'left':",
        "if not matches:",
        "inner join drops unmatched left rows",
    )
    return FamilyDefinition(
        "table_join",
        "train",
        "left/right are lists of [key,value] rows. mode is left or inner (otherwise ValueError). "
        "Return [key,left_value,right_value] rows for every match, preserving left order then right order. "
        "Duplicate keys form all combinations. Left join alone emits [key,left_value,None] for missing "
        "right matches. Empty inputs are valid; never mutate either input.",
        (
            (
                "indexing.py",
                """
                def build_index(rows):
                    index = {}
                    for key, value in rows:
                        index.setdefault(key, []).append(value)
                    return index
            """,
            ),
            (
                "joining.py",
                """
                def join_rows(left, index, mode):
                    result = []
                    for key, left_value in left:
                        matches = index.get(key, [])
                        if not matches and mode == 'left':
                            result.append([key, left_value, None])
                        for right_value in matches:
                            result.append([key, left_value, right_value])
                    return result
            """,
            ),
            (
                "modes.py",
                """
                def checked_mode(mode):
                    if mode not in ('left', 'inner'):
                        raise ValueError('mode')
                    return mode
            """,
            ),
        ),
        "from indexing import build_index\nfrom joining import join_rows\nfrom modes import checked_mode",
        "join_rows(payload['left'], build_index(payload['right']), checked_mode(payload['mode']))",
        ("left", "right", "mode"),
        ((duplicates, mode), (order, mode)),
        (
            Mutation(
                "joining.py",
                "result.append([key, left_value, None])",
                "result.append([key, None, None])",
                "unmatched left value is retained",
            ),
            Mutation(
                "joining.py",
                "for key, left_value in left:",
                "for key, left_value in reversed(left):",
                "left input order retained",
            ),
        ),
        """
        p = {'left':[['z',1],['a',2],['a',3]], 'right':[['a',4],['a',5]], 'mode':'left'}
        assert solve(p) == [['z',1,None],['a',2,4],['a',2,5],['a',3,4],['a',3,5]], 'contract: combinations/order/unmatched'
        assert solve({'left':[['x',6]], 'right':[], 'mode':'inner'}) == [], 'contract: inner'
        assert solve({'left':[], 'right':[], 'mode':'left'}) == [], 'contract: empty'
        """,
        """
        p = {'left':[['b','L'],['c','M']], 'right':[['b','R2'],['b','R1'],['c','C'],['d','unused']], 'mode':'inner'}
        assert solve(p) == [['b','L','R2'],['b','L','R1'],['c','M','C']]
        assert p['left'] == [['b','L'],['c','M']]
        assert solve({'left':[['w','keep']], 'right':[], 'mode':'left'}) == [['w','keep',None]]
        assert solve({'left':[['v','drop']], 'right':[], 'mode':'inner'}) == []
        try:
            solve({'left':[], 'right':[], 'mode':'outer'})
        except ValueError:
            pass
        else:
            raise AssertionError('invalid mode')
        """,
    )


def record_diff() -> FamilyDefinition:
    removed_field = Mutation(
        "changes.py", "set(old) | set(new)", "set(new)", "deleted fields count as changes"
    )
    added_field = Mutation(
        "changes.py", "set(old) | set(new)", "set(old)", "new fields count as changes"
    )
    removed_record = Mutation(
        "partition.py", "key not in new", "key in new", "removed IDs absent from new records"
    )
    return FamilyDefinition(
        "record_diff",
        "test",
        "old/new are lists of records with unique string id per list and arbitrary other string fields. "
        "Return {added:[id], removed:[id], updated:[[id,[changed_field,...]]]}: added and updated follow "
        "new-record order, removed follows old-record order; changed fields sorted, excluding id. "
        "Missing field differs from explicit None. Values compare by Python equality. No input mutation.",
        (
            (
                "indexing.py",
                """
                def index_records(records):
                    return {record['id']: record for record in records}
            """,
            ),
            (
                "changes.py",
                """
                def changed_fields(old, new):
                    fields = (set(old) | set(new)) - {'id'}
                    return sorted([key for key in fields if key not in old or key not in new or old[key] != new[key]])
            """,
            ),
            (
                "partition.py",
                """
                from changes import changed_fields

                def compare(old, new):
                    added = [key for key in new if key not in old]
                    removed = [key for key in old if key not in new]
                    updated = []
                    for key in new:
                        if key in old:
                            changed = changed_fields(old[key], new[key])
                            if changed:
                                updated.append([key, changed])
                    return {'added': added, 'removed': removed, 'updated': updated}
            """,
            ),
        ),
        "from indexing import index_records\nfrom partition import compare",
        "compare(index_records(payload['old']), index_records(payload['new']))",
        ("old", "new"),
        ((removed_field, removed_record), (added_field, removed_record)),
        (
            Mutation(
                "changes.py",
                "key not in old or key not in new or old[key] != new[key]",
                "old.get(key) != new.get(key)",
                "missing differs from None",
            ),
            Mutation(
                "partition.py",
                "added = [key for key in new if key not in old]",
                "added = sorted([key for key in new if key not in old])",
                "added IDs follow input order",
            ),
        ),
        """
        p = {'old':[{'id':'r','gone':1}, {'id':'s'}], 'new':[{'id':'r','extra':2}, {'id':'z'}, {'id':'a'}]}
        assert solve(p) == {'added':['z','a'], 'removed':['s'], 'updated':[['r',['extra','gone']]]}, 'contract: delta/order'
        assert solve({'old':[{'id':'n','value':None}], 'new':[{'id':'n'}]})['updated'] == [['n',['value']]], 'contract: None/missing'
        assert solve({'old':[], 'new':[]}) == {'added':[], 'removed':[], 'updated':[]}, 'contract: empty'
        """,
        """
        p = {'old':[{'id':'b','x':0}, {'id':'a','x':1}], 'new':[{'id':'a','x':3}, {'id':'b','x':0}]}
        assert solve(p) == {'added':[], 'removed':[], 'updated':[['a',['x']]]}
        assert p['old'][1] == {'id':'a','x':1}
        assert solve({'old':[{'id':'q'}], 'new':[{'id':'q','v':None}]})['updated'] == [['q',['v']]]
        assert solve({'old':[{'id':'j','v':None}], 'new':[{'id':'j'}]})['updated'] == [['j',['v']]]
        assert solve({'old':[], 'new':[{'id':'y'},{'id':'b'}]})['added'] == ['y','b']
        """,
    )
