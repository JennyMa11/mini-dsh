"""Optional local browser presentation, independent of the core runtime."""

from minidsh.gui.server import handle_connection, serve_gui
from minidsh.gui.session import GuiSession

__all__ = ["GuiSession", "handle_connection", "serve_gui"]
