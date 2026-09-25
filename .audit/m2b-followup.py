from pathlib import Path


def change(path, old, new):
    file = Path(path)
    text = file.read_text()
    if new in text:
        return
    assert old in text, path
    file.write_text(text.replace(old, new, 1))


change('tests/test_walk_branch.py', '    import test_walk_forward as TW',
       '    from tests import test_walk_forward as TW')
