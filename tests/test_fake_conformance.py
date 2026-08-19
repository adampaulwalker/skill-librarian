"""The tripwire on the mistake that let three real bugs through unnoticed.

Every test in this suite talks to a stand-in for GitHub rather than to GitHub. That is the
only sensible way to test this service, and it has one failure mode that is worse than
having no tests at all: a stand-in that promises less than the real client promises. When
that happens the suite still passes, everybody reads the green result as proof, and the
behaviour the tests were written to guarantee is simply missing from production.

That is not a hypothetical. A stand-in whose merge did not have to name the change it was
publishing hid the fact that anyone with write access could swap the wording between the
moment a person agreed to it and the moment it went out. The tests passed the whole time.

So this file does not test the service. It tests the stand-ins: every class anywhere in the
test suite that offers a method the real client offers has to spell that method the same way
the real contract spells it. A stand-in whose merge quietly makes the approved commit
optional fails here, at the moment somebody writes it, rather than years later in a
repository somebody cares about.

The search is done by reading the test files rather than by importing them and looking at
what came out. That is deliberate. A stand-in is quite often written inside the test that
needs it, and a class defined inside a function does not exist until that function runs, so
importing the module would walk straight past it. Reading the source finds a class wherever
it is written.

Only the shape of each method is checked here. Whether a stand-in also *behaves* the way the
real client behaves is checked alongside the real client, in ``test_github.py``.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from librarian.github import GitHubClient

TESTS_ROOT = pathlib.Path(__file__).resolve().parent
THIS_FILE = pathlib.Path(__file__).name

#: How the real contract spells each method, taken from the Protocol itself rather than
#: written out again here. Copying it out would just be a second thing to drift.
CONTRACT: dict[str, inspect.Signature] = {
    name: inspect.signature(getattr(GitHubClient, name))
    for name in dir(GitHubClient)
    if not name.startswith("_") and callable(getattr(GitHubClient, name, None))
}

#: Methods a stand-in may add for the tests' own convenience without claiming to be the
#: real client. They are not part of the contract, so they are not checked against it.
#:
#: This is deliberately empty. It once held ``delete_branch``, written down while taking a
#: working copy away was something a client could choose not to offer. That exemption is
#: exactly how the drift got in: the publisher called it, no real client had it, and every
#: stand-in was excused from being checked, so nothing anywhere said a word. Anything added
#: back here has to be something no production client will ever be asked for.
NOT_PART_OF_THE_CONTRACT: frozenset[str] = frozenset()


def test_nothing_in_the_real_contract_is_excused_from_being_checked() -> None:
    """An exemption naming a real method turns this whole file into a rubber stamp."""
    excused = sorted(NOT_PART_OF_THE_CONTRACT & set(CONTRACT))
    assert excused == [], (
        f"these are part of the real contract but excused from every check below: {excused}. "
        "A stand-in may then spell them any way it likes, or leave them out entirely, which "
        "is the drift this file exists to catch."
    )


def _shape_of_signature(signature: inspect.Signature) -> list[tuple[str, str, bool]]:
    """A real signature reduced to name, kind, and whether the argument may be left out."""
    shape = []
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            continue
        shape.append(
            (parameter.name, str(parameter.kind), parameter.default is not inspect.Parameter.empty)
        )
    return shape


def _shape_of_ast(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str, bool]]:
    """The same reduction, read out of the source rather than out of a live function.

    Type annotations are ignored on purpose: a stand-in may write ``dict`` where the
    contract writes ``dict[str, str]`` without promising anything different. A default value
    is a different matter entirely. It is a promise that a caller may leave the argument out,
    and the real client makes no such promise about the commit a merge is allowed to publish.
    """
    args = node.args
    shape: list[tuple[str, str, bool]] = []

    positional = list(args.posonlyargs) + list(args.args)
    first_with_default = len(positional) - len(args.defaults)
    for index, argument in enumerate(positional):
        if argument.arg == "self":
            continue
        kind = (
            "POSITIONAL_ONLY"
            if index < len(args.posonlyargs)
            else "POSITIONAL_OR_KEYWORD"
        )
        shape.append((argument.arg, kind, index >= first_with_default))

    if args.vararg is not None:
        shape.append((args.vararg.arg, "VAR_POSITIONAL", False))
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        shape.append((argument.arg, "KEYWORD_ONLY", default is not None))
    if args.kwarg is not None:
        shape.append((args.kwarg.arg, "VAR_KEYWORD", False))
    return shape


def _stand_in_methods() -> list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every method, anywhere in the test suite, that claims to be part of the contract.

    Walks the whole tree of each file, so a class written inside a test function is found
    exactly like one written at the top of the file.
    """
    found: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path in sorted(TESTS_ROOT.glob("*.py")):
        if path.name == THIS_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name in NOT_PART_OF_THE_CONTRACT or item.name not in CONTRACT:
                    continue
                found.append((f"{path.name}::{node.name}", item.name, item))
    return found


def test_the_stand_ins_are_actually_being_found() -> None:
    """A search that finds nothing would pass every check below in silence."""
    where = {location for location, _method, _node in _stand_in_methods()}
    assert "fakes.py::FakeGitHubClient" in where, where
    assert "test_publisher.py::LocalFakeGitHub" in where, where
    # The stand-in written inside a test function. If this disappears from the list, the
    # search has stopped looking inside functions and the blind spot is back.
    assert "test_service.py::NoRecordedHistory" in where, where
    assert len(where) >= 6, f"only found {sorted(where)}"


def test_the_contract_was_read_from_the_protocol() -> None:
    """The list of methods is taken from the real contract, so it cannot fall behind it."""
    assert {"merge_pr", "commit_files", "create_branch", "open_pr"} <= set(CONTRACT)
    assert _shape_of_signature(CONTRACT["merge_pr"]) == [
        ("number", "POSITIONAL_OR_KEYWORD", False),
        ("commit_title", "POSITIONAL_OR_KEYWORD", False),
        ("expected_head_sha", "POSITIONAL_OR_KEYWORD", False),
    ], "a merge has to name the approved commit, and it is never optional"


@pytest.mark.parametrize(
    "location,method_name,node",
    _stand_in_methods(),
    ids=[f"{location}.{method}" for location, method, _node in _stand_in_methods()],
)
def test_a_stand_in_spells_every_method_the_way_the_real_contract_spells_it(
    location: str, method_name: str, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> None:
    """A stand-in that asks for less than the real client is a hole in the whole suite."""
    expected = _shape_of_signature(CONTRACT[method_name])
    actual = _shape_of_ast(node)
    assert actual == expected, (
        f"{location}.{method_name} does not match the real client, so the suite could pass "
        f"while production behaved differently.\n"
        f"  contract: {expected}\n"
        f"  stand-in: {actual}"
    )


def test_no_stand_in_makes_the_approved_commit_optional() -> None:
    """Said on its own because this exact drift hid a real bug.

    A merge that may be called without naming the commit that was approved will happily
    publish whatever the working copy holds by the time it runs. Every stand-in has to
    refuse that the same way the real client does.
    """
    offenders = []
    for location, method_name, node in _stand_in_methods():
        if method_name != "merge_pr":
            continue
        named = [entry for entry in _shape_of_ast(node) if entry[0] == "expected_head_sha"]
        if not named or named[0][2]:
            offenders.append(location)
    assert offenders == [], f"these stand-ins let a merge skip the approved commit: {offenders}"


def test_every_stand_in_that_saves_work_settles_the_shared_branch_question() -> None:
    """A commit onto the branch everyone reads from reaches nobody, so it must never happen.

    There are two honest ways for a stand-in to hold that line, and this insists on one of
    them rather than on a single house style, because both are in use here for good reason.

    The first is to refuse, the way the real client refuses. That is right for the shared
    fake in ``fakes.py``, which nearly every test in the suite leans on.

    The second is to accept it and let a test in the same file watch what the publisher
    actually does. ``test_publisher.py`` takes this route on purpose: a stand-in that
    refuses can only ever prove that refusing works, whereas a permissive one paired with
    ``test_publish_never_writes_to_the_default_branch`` proves the stronger and more useful
    thing, which is that the publisher never asks in the first place.

    What is not acceptable is neither: a stand-in that quietly accepts a commit onto the
    shared branch while nothing in its file ever checks where the commits went.
    """
    unsettled = []
    for path in sorted(TESTS_ROOT.glob("*.py")):
        if path.name == THIS_FILE:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        # Does something in this file check which branch the commits landed on?
        watched = '("commit_files", DEFAULT_BRANCH)' in source or (
            'commit["branch"] != DEFAULT_BRANCH' in source
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            saves = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.FunctionDef) and item.name == "commit_files"
                ),
                None,
            )
            if saves is None:
                continue
            written = ast.get_source_segment(source, saves) or ""
            refuses = "_is_shared_branch" in written
            delegates = "super().commit_files" in written
            if refuses or delegates or watched:
                continue
            unsettled.append(f"{path.name}::{node.name}")
    assert unsettled == [], (
        "these stand-ins accept a commit onto the shared branch and nothing in their file "
        f"ever checks where the commits went, so the refusal could be lost silently: {unsettled}"
    )


def test_the_shared_fake_itself_refuses_the_shared_branch() -> None:
    """Said separately because almost every test in the suite leans on this one stand-in.

    ``fakes.py`` is not paired with a test watching where its commits go, so it has to hold
    the line by refusing, exactly as the real client does.
    """
    source = (TESTS_ROOT / "fakes.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="fakes.py")
    saves = [
        item
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "FakeGitHubClient"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "commit_files"
    ]
    assert saves, "the shared fake no longer saves work at all"
    written = ast.get_source_segment(source, saves[0]) or ""
    assert "_is_shared_branch" in written, (
        "the shared fake stopped refusing the branch everyone reads from, so a publish path "
        "could write straight to it and nearly every test in the suite would stay green"
    )
