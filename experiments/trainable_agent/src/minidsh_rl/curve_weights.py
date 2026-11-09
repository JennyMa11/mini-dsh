"""Owned immutable checkpoint download with verified TLS and bounded read-only polling."""

import asyncio
import ssl
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pytrio.config.config import auth_state  # type: ignore[import-untyped]
from pytrio.transport.control.http_client import ControlHTTPClient  # type: ignore[import-untyped]

MAX_BYTES = 128 * 1024 * 1024


async def download_owned(run_id: str, uri: str, output: Path) -> None:
    """No save/update calls; URLs and credentials must never enter logs or reports."""
    if await asyncio.to_thread(output.exists) or await asyncio.to_thread(output.is_symlink):
        raise ValueError("fresh download path required")
    client = ControlHTTPClient(api_key=auth_state.api_key)
    try:
        auth = await client.verify_api_key()
        run = await client.get_training_run(run_id, access_scope="owned")
        if run.workspace_cuid != auth.workspace_cuid or run.base_model != "Qwen/Qwen3.5-4B":
            raise ValueError("checkpoint owner/model mismatch")
        checkpoint = None
        for _ in range(8):
            listing = await client.list_training_run_checkpoints(run_id)
            matches = [cp for cp in listing.checkpoints if cp.checkpoint_uri == uri]
            if len(matches) == 1 and matches[0].status == "READY":
                checkpoint = matches[0]
                break
            await asyncio.sleep(2)
        if checkpoint is None:
            raise ValueError("checkpoint read-only readiness deadline")
        if (
            checkpoint.training_run_cuid != run_id
            or checkpoint.workspace_cuid not in (None, auth.workspace_cuid)
            or f"/{auth.workspace_cuid}/{run_id}/" not in checkpoint.path
        ):
            raise ValueError("checkpoint ownership association mismatch")
        response = await client.get_checkpoint_download_url(checkpoint.checkpoint_cuid)
        if urlparse(response.url).scheme != "https":
            raise ValueError("checkpoint requires HTTPS")
        context = ssl.create_default_context()
        if response.tls_cert_pem:
            context.load_verify_locations(cadata=response.tls_cert_pem)
        temporary = output.with_suffix(".partial")
        count = 0
        async with (
            httpx.AsyncClient(verify=context, timeout=60, follow_redirects=False) as transport,
            transport.stream("GET", response.url) as stream,
        ):
            stream.raise_for_status()
            with temporary.open("xb") as file:
                temporary.chmod(0o600)
                async for block in stream.aiter_bytes():
                    count += len(block)
                    if count > MAX_BYTES:
                        raise ValueError("checkpoint exceeds download bound")
                    file.write(block)
        if not count:
            raise ValueError("empty checkpoint")
        temporary.replace(output)
    finally:
        await client.aclose()
