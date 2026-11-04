from pathlib import Path
scope = {'__name__': '__main__'}
exec(compile(Path('solution.py').read_text(), 'solution.py', 'exec'), scope)
exec(compile('def check(has_close_elements):\n    assert has_close_elements([1.0, 2.0, 3.0], 0.5) == False\n    assert has_close_elements([1.0, 2.8, 3.0, 4.0, 5.0, 2.0], 0.3) == True\ncheck(has_close_elements)\n', '<tests>', 'exec'), scope)
print('examples_passed')
