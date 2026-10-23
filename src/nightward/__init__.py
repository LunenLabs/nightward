"""nightward — regression firewall for AI-driven changes.

Surface: stop the exhausting infinite fix-loop.
Subtitle: gate every AI-made change against an approved behavior boundary.
"""

__version__ = "0.3.4"


def capture(fn=None, *, name: str | None = None, group: str | None = None,
            semantic: bool = False, scrub: bool = True):
    """Capture a test's return value as a behavior.

        @capture(group="billing")
        def test_checkout():
            return checkout_total(CART)

    is the same as `behavior("test_checkout", checkout_total(CART), group="billing")`.
    The name is the test's name unless `name=` is given; a parametrized case keeps
    its id (`test_tax[low]`, or `tax[low]` with name="tax"). semantic and scrub mean
    what they mean for the `behavior` fixture. A test that returns None fails: it
    almost always forgot its `return`. Async tests work under an async plugin
    (pytest-asyncio, anyio); async generator tests are refused.
    """
    from .pytest_plugin import capture_mark  # pytest only where tests run
    return capture_mark(fn, name=name, group=group, semantic=semantic, scrub=scrub)
