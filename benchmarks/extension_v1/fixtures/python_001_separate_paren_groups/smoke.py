from pathlib import Path
scope = {'__name__': '__main__'}
exec(compile(Path('solution.py').read_text(), 'solution.py', 'exec'), scope)
exec(compile("def check(separate_paren_groups):\n    assert separate_paren_groups('( ) (( )) (( )( ))') == ['()', '(())', '(()())']\ncheck(separate_paren_groups)\n", '<tests>', 'exec'), scope)
print('examples_passed')
