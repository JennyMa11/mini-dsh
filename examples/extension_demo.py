"""Offline PR1 audit/injection examples using the existing plugin composition.

Run: uv run python examples/extension_demo.py
No model service, shell command or workspace writes are needed by the agent.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, RuntimeServices, plugin_runtime
from minidsh.domain import Message, ModelRequest, ModelResponse, Role, RunStatus
from minidsh.domain.extensions import HookView
from minidsh.runtime import PluginManifest, ServiceRegistry
from minidsh.runtime.hooks import HookRegistry


class ExampleExtension:
    """A third-party plugin; registration belongs to the ordinary lifecycle."""

    manifest = PluginManifest("example_extension", "1.0", provides=("example_hooks",))

    def __init__(self, *, inject: bool) -> None:
        self._inject = inject
        self.hooks = HookRegistry()
        self.audit: list[str] = []

    async def start(self, services: ServiceRegistry) -> None:
        services.register("example_hooks", self.hooks)
        services.on_cleanup(self.hooks.register_v2("audit", "1.0", on_run_start=self._started))
        if self._inject:
            services.on_cleanup(
                self.hooks.register_v2("injection", "1.0", before_model=self._before_model)
            )

    async def _started(self, state: HookView) -> None:
        self.audit.append(state.run_id)

    async def _before_model(self, request: ModelRequest, _state: HookView) -> ModelRequest:
        note = Message(
            Role.USER, "Derived project note: keep the answer concise.", name="example_note"
        )
        return replace(request, messages=(*request.messages, note))

    async def stop(self) -> None:
        """ServiceRegistry runs the registered removals after this callback."""


async def main() -> None:
    """Show pure audit and derived request injection without editing the loop."""
    with TemporaryDirectory(prefix="minidsh-extension-") as directory:
        for inject in (False, True):
            extension = ExampleExtension(inject=inject)
            model = ScriptedModel([ModelResponse("Done.")])
            async with plugin_runtime(
                workspace=Path(directory),
                options=RuntimeOptions(),
                model=model,
                plugins=(extension,),
                selection=RuntimeServices(hooks="example_hooks"),
            ) as runtime:
                result = await runtime.agent.run("Answer briefly.")
                assert result.status is RunStatus.FINISHED
                assert len(extension.audit) == 1
                assert any(m.name == "example_note" for m in model.requests[0].messages) == inject
                assert all(m.name != "example_note" for m in result.history)
            assert extension.hooks.order_manifest() == ()
            print(f"{'injection' if inject else 'audit'}: finished; plugin cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
