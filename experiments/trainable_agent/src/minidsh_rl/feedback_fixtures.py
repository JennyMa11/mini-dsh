"""Opt-in development drafts; no original task or independent checker changes."""

from dataclasses import replace
from textwrap import dedent

from minidsh_rl.minirepo_tasks import MiniTask

_EXAMPLES = {
    "inventory": """
        feedback_payload = {'stock': {'feedback_part': 2}, 'orders': [['feedback_part', 3]]}
        assert solve(feedback_payload) == {
            'accepted': False, 'remaining': {'feedback_part': 2},
            'shortages': {'feedback_part': 1}}, 'contract: insufficient stock rejects the request'
        assert feedback_payload['stock'] == {'feedback_part': 2}, 'contract: input ownership'
    """,
    "windows": """
        feedback_payload = {'events': [
            ['feedback_event', 3, ' Alpha '], ['feedback_event', 3, ' Beta ']],
            'start': 2, 'end': 4}
        assert solve(feedback_payload) == {'counts': {'beta': 1}, 'total': 1}, (
            'contract: last input wins equal-timestamp ties')
    """,
    "settings": """
        feedback_payload = {'base': {'feedback_feature': 7},
            'override': {'feedback_feature': None}, 'paths': ['feedback_feature', 'absent']}
        assert solve(feedback_payload) == {
            'config': {'feedback_feature': None}, 'values': [None, 'MISSING']}, (
            'contract: None is a value, not a missing path')
    """,
    "billing": """
        feedback_payload = {'lines': [
            {'sku': 'feedback_part', 'price': 3, 'quantity': 1, 'discount': 0}], 'tax_rate': 1}
        assert solve(feedback_payload) == {
            'by_sku': {'feedback_part': 3}, 'net': 3, 'tax': 1, 'total': 4}, (
            'contract: positive fractional invoice tax rounds upward')
    """,
}


def draft_feedback_tasks(tasks: tuple[MiniTask, ...]) -> tuple[MiniTask, ...]:
    """Append public-spec examples under new IDs; never read hidden tests or gold code."""
    drafts = []
    marker = "print('public tests passed')\n"
    for task in tasks:
        if task.case_id.endswith("_feedback_v2"):
            raise ValueError("draft must start from an original task")
        files = dict(task.files)
        public = files["public_tests.py"]
        if public.count(marker) != 1:
            raise ValueError("expected one final public-test marker")
        extra = dedent(_EXAMPLES[task.family]).strip() + "\n"
        files["public_tests.py"] = public.replace(marker, extra + marker, 1)
        drafts.append(
            replace(task, case_id=task.case_id + "_feedback_v2", files=tuple(sorted(files.items())))
        )
    return tuple(drafts)
