from pathlib import Path
scope = {'__name__': '__main__'}
exec(compile(Path('solution.py').read_text(), 'solution.py', 'exec'), scope)
exec(compile("\n\n\n\n\ndef check(separate_paren_groups):\n    assert separate_paren_groups('(()()) ((())) () ((())()())') == [\n        '(()())', '((()))', '()', '((())()())'\n    ]\n    assert separate_paren_groups('() (()) ((())) (((())))') == [\n        '()', '(())', '((()))', '(((())))'\n    ]\n    assert separate_paren_groups('(()(())((())))') == [\n        '(()(())((())))'\n    ]\n    assert separate_paren_groups('( ) (( )) (( )( ))') == ['()', '(())', '(()())']\n\ncheck(separate_paren_groups)", '<tests>', 'exec'), scope)
print('independent_passed')
