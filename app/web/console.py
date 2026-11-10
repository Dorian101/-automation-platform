import logging

from aiohttp import web

from app.core.results import CommandError

from .live import stream as live_stream
from .live import tab_of
from .pages import render_plugin_page
from .responses import redirect as _redirect

logger = logging.getLogger(__name__)


def _render_plugin_page(page_path: str, view: dict, nav_html: str) -> web.Response:
    """Build a plugin's page.

    The painting lives in :mod:`app.web.pages`, which the demonstration uses
    as well: the same plugins, the same renderer, a different identity in
    front of them.
    """
    return web.Response(
        text=render_plugin_page(page_path, view, nav_html),
        content_type="text/html",
    )


class ConsoleRoutes:
    async def _plugin_page(self, request: web.Request) -> web.Response:
        """A plugin's own page, rendered from the description it returned.

        The plugin answers with a dict; nothing here knows what a plugin
        shows. A route only exists for a plugin that declared it, so an
        unknown path falls through to the console rather than reaching a
        handler that has no owner.
        """
        plugin = self._manager.find_by_page(request.path)

        if plugin is None:
            return _redirect("/")

        identity = self._identity(request)

        if identity is None:
            return self._login_required(request)

        try:
            view = await plugin.page_view(identity)
        except CommandError as error:
            return web.Response(text=str(error), content_type="text/plain", status=400)
        except Exception:
            logger.exception("Page %s failed to build", plugin.name)
            return web.Response(
                text="Internal error",
                content_type="text/plain",
                status=500,
            )

        return _render_plugin_page(plugin.page, view, self._nav(request))

    async def _plugin_page_action(self, request: web.Request) -> web.Response:
        """Something was done on a plugin page; answer with it redrawn.

        Every value arrives as a string and is converted by the plugin, which
        is the only place that knows what a valid amount or a valid month is.
        The target page is named in the body rather than in the path, because
        the action endpoint is one shared route.
        """
        identity = self._identity(request)

        if identity is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Request body must be JSON"},
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                {"error": "Request body must be a JSON object"},
                status=400,
            )

        page = payload.get("page", "")
        action = payload.get("action", "")
        body = payload.get("payload", {})

        if not isinstance(page, str) or not isinstance(action, str):
            return web.json_response(
                {"error": "'page' and 'action' must be strings"},
                status=400,
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"error": "'payload' must be an object"},
                status=400,
            )

        plugin = self._manager.find_by_page(page)

        if plugin is None:
            return web.json_response({"error": "Unknown page"}, status=404)

        try:
            view = await plugin.page_action(identity, action, body)
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Action %s failed on %s", action, plugin.name)
            return web.json_response({"error": "Internal error"}, status=500)

        if action != "view":
            plugin.changed(identity, tab_of(request))

        return web.json_response({"view": view})

    async def _events(self, request: web.Request) -> web.StreamResponse:
        identity = self._identity(request)

        if identity is None:
            return web.json_response({"error": "Unauthorized"}, status=401)

        return await live_stream(
            request,
            self._live,
            identity,
            lambda: self._identity(request) == identity,
            self._is_admin(request),
        )

    async def _commands(self, request: web.Request) -> web.Response:
        """The commands worth typing.

        A plugin that owns a page is left out. Its commands exist only so the
        router can refuse them with "web console only" rather than "unknown
        command", and listing them here would offer somebody something that
        cannot work: the page is in the menu, and the two say the same thing
        twice.

        The filter is here rather than in the page because the answer is a
        fact about the plugin, and a page that hid what it was given would
        need to know the rule to hide it.
        """
        plugins = [
            {
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "commands": plugin.commands,
                "command_args": {
                    cmd: [{"type": t, "label": label} for t, label in fields]
                    for cmd, fields in plugin.command_args.items()
                },
            }
            for plugin in self._manager.get_plugins()
            if not plugin.web_only and plugin.commands
        ]

        return web.json_response({"plugins": plugins})

    async def _execute(self, request: web.Request) -> web.Response:
        identity = self._identity(request)

        if identity is None:
            return self._login_required(request)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Request body must be JSON"},
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                {"error": "Request body must be a JSON object"},
                status=400,
            )

        command = payload.get("command", "")
        args = payload.get("args", "")

        if not isinstance(command, str) or not isinstance(args, str):
            return web.json_response(
                {"error": "'command' and 'args' must be strings"},
                status=400,
            )

        try:
            result = await self._manager.execute(command.strip(), args, identity)
        except CommandError as error:
            return web.json_response({"error": str(error)}, status=400)
        except Exception:
            logger.exception("Command %s failed", command)
            return web.json_response(
                {"error": "Internal error"},
                status=500,
            )

        # data is included only when the plugin produced it, so a response
        # from a plugin that has nothing structured to say keeps the exact
        # shape it had before the field existed.
        body = {"result": result.text}

        if result.data is not None:
            body["data"] = result.data

        return web.json_response(body)
