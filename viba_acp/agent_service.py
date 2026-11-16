"""The service side of the `viba_acp` interface: the ACP backend's operations.

`new_session` opens the conversation this process will use and answers its `Session`
(an `AcpBackend` value and the session id); `send_prompt` runs one prompt turn on it
and answers the `PromptResponse`. Both go through `service.recorded`, so every call is
recorded under its storage path and a later run on the same store replays it instead of
prompting the agent again.

The conversation — one agent process, and the ACP sessions that process has live — lives
in this process and is closed when the process exits. `Session` is data: it names the
backend and the session the calls use.

`VIBA_AGENT` picks the agent (`scripted`, `claude`, `dsh`); `agents.py` holds each
command and the environment it needs. The interface, the program and the entry do
not change.
"""

import atexit
import os
import sys
from pathlib import Path

from viba import viba_ast
from viba.interpret import viba_data
from viba.reflect import VObject, VibaReflectError

import acp_client
from agents import the_command
from distributed.service import leaf_value, run_service
from viba.interpret import _HostArgument, sub_env

HERE = Path(__file__).resolve().parent    # viba_acp
WORKSPACE = HERE.parent                   # the repository root

# What this service drives. `VIBA_AGENT` picks it (`scripted`, `claude`, `dsh`);
# `agents.py` knows each command and the environment it needs.
AGENT, AGENT_ENV = the_command()

# The `AcpBackend` variant each agent is.
_BACKEND_TAGS = {
    "scripted": "$ScriptedAgentBackend",
    "claude": "$ClaudeAgentBackend",
    "dsh": "$DshAgentBackend",
}

# One conversation per agent command, for the life of this service process.
_CONVERSATIONS = {}


def the_api(service):
    """The interface's table: one function per operation the module declares."""

    def new_session(environ):
        return service.recorded(
            environ, lambda: the_session_value(the_conversation().session))

    def send_prompt(environ, session, prompt_blocks, on_update):
        session = the_session_id(session)
        blocks = [the_block(block) for block in prompt_blocks]
        handler = the_handler(on_update, environ)

        def turn():
            conversation = the_conversation()
            conversation.find_session(session)
            return the_response(conversation, session, blocks, handler)

        return service.recorded(environ, turn)

    def resume_session(environ, session_id, cwd):
        session_id = leaf_value(session_id)
        cwd = leaf_value(cwd)

        def resume():
            the_conversation().resume_session(session_id, cwd)
            return the_session_value(session_id)

        return service.recorded(environ, resume)

    def load_session(environ, session_id, cwd, on_update):
        session_id = leaf_value(session_id)
        cwd = leaf_value(cwd)
        handler = the_handler(on_update, environ)

        def load():
            the_conversation().load_session(session_id, cwd, handler)
            return the_session_value(session_id)

        return service.recorded(environ, load)

    def fork_session(environ, session, cwd):
        session = the_session_id(session)
        cwd = leaf_value(cwd)

        def fork():
            forked = the_conversation().fork_session(session, cwd)
            return the_session_value(forked)

        return service.recorded(environ, fork)

    def on_notification(environ, notification):
        service.recorded(environ, lambda: notification)
        return viba_data(viba_ast.Nil())

    def cancel_session(environ, session):
        the_conversation().cancel_session(the_session_id(session))
        return viba_data(viba_ast.Nil())

    def close_session(environ, session):
        the_conversation().close_session(the_session_id(session))
        return viba_data(viba_ast.Nil())

    def delete_session(environ, session):
        the_conversation().delete_session(the_session_id(session))
        return viba_data(viba_ast.Nil())

    def archive_session(environ, session):
        the_conversation().archive_session(the_session_id(session))
        return viba_data(viba_ast.Nil())

    def unarchive_session(environ, session):
        the_conversation().unarchive_session(the_session_id(session))
        return viba_data(viba_ast.Nil())

    def set_session_title(environ, session, title):
        the_conversation().set_session_title(the_session_id(session),
                                             leaf_value(title))
        return viba_data(viba_ast.Nil())

    return {"new_session": new_session, "send_prompt": send_prompt,
            "resume_session": resume_session, "load_session": load_session,
            "fork_session": fork_session,
            "cancel_session": cancel_session, "close_session": close_session,
            "delete_session": delete_session, "archive_session": archive_session,
            "unarchive_session": unarchive_session,
            "set_session_title": set_session_title,
            "on_notification": on_notification}


def the_conversation():
    """The conversation this process has with that agent, made on first use."""
    key = tuple(AGENT)
    conversation = _CONVERSATIONS.get(key)
    if conversation is None:
        conversation = acp_client.Conversation(
            AGENT, WORKSPACE,
            permissions=os.environ.get("VIBA_AGENT_PERMISSION", "reject"),
            trace=the_trace(), env=AGENT_ENV)
        _CONVERSATIONS[key] = conversation
        atexit.register(conversation.close)
    return conversation


def the_block(block):
    """One viba content block as the ACP wire form."""
    annotations = the_optional(block, "$annotations")
    if the_has(block, "$TextContent"):
        piece = {"type": "text", "text": leaf_value(block.by_tag("$text"))}
        if annotations is not None:
            piece["annotations"] = the_annotations(annotations)
        return piece
    if the_has(block, "$ImageContent"):
        piece = {"type": "image",
                 "data": leaf_value(block.by_tag("$data")),
                 "mimeType": leaf_value(block.by_tag("$mime_type"))}
        uri = the_optional(block, "$uri")
        if uri is not None:
            piece["uri"] = uri
        if annotations is not None:
            piece["annotations"] = the_annotations(annotations)
        return piece
    if the_has(block, "$AudioContent"):
        piece = {"type": "audio",
                 "data": leaf_value(block.by_tag("$data")),
                 "mimeType": leaf_value(block.by_tag("$mime_type"))}
        if annotations is not None:
            piece["annotations"] = the_annotations(annotations)
        return piece
    if the_has(block, "$ResourceLink"):
        piece = {"type": "resource_link",
                 "name": leaf_value(block.by_tag("$name")),
                 "uri": leaf_value(block.by_tag("$uri"))}
        for tag, key in (("$title", "title"), ("$description", "description"),
                         ("$mime_type", "mimeType"), ("$size", "size")):
            value = the_optional(block, tag)
            if value is not None:
                piece[key] = value
        if annotations is not None:
            piece["annotations"] = the_annotations(annotations)
        return piece
    if the_has(block, "$EmbeddedResource"):
        resource = block.by_tag("$resource")
        contents = {"uri": leaf_value(resource.by_tag("$uri"))}
        mime = the_optional(resource, "$mime_type")
        if mime is not None:
            contents["mimeType"] = mime
        blob = the_optional(resource, "$blob")
        if blob is not None:
            contents["blob"] = blob
        else:
            contents["text"] = leaf_value(resource.by_tag("$text"))
        piece = {"type": "resource", "resource": contents}
        if annotations is not None:
            piece["annotations"] = the_annotations(annotations)
        return piece
    raise RuntimeError("the content block is none of the five variants")


def the_has(block, tag) -> bool:
    """Whether this piece carries the member `tag`."""
    try:
        block.by_tag(tag)
        return True
    except VibaReflectError:
        return False


def the_annotations(value):
    """One `Annotations` as the ACP wire form."""
    piece = {}
    audience = the_optional(value, "$audience")
    if audience is not None:
        piece["audience"] = [leaf_value(role) for role in audience]
    last_modified = the_optional(value, "$last_modified")
    if last_modified is not None:
        piece["lastModified"] = last_modified
    priority = the_optional(value, "$priority")
    if priority is not None:
        piece["priority"] = priority
    return piece


def the_optional(block, tag):
    """One optional member: its leaf, or None when it is nil or absent."""
    try:
        return leaf_value(block.by_tag(tag))
    except VibaReflectError:
        return None


def the_handler(on_update, environ):
    """The viba function the client calls for each `session/update`, or None."""
    if isinstance(on_update, VObject) and isinstance(on_update.data, viba_ast.Nil):
        return None

    count = [0]

    def handler(notification):
        piece = the_notification(notification)
        if piece is None:
            return
        count[0] += 1
        # A function-typed argument is handed over as the call it stands for, and
        # that call answers at most once. Each notification is its own call, in a
        # child environment of its own: a fresh argument object per notification.
        call = _HostArgument(on_update.node, on_update.scope, on_update.pending,
                             on_update.module, on_update.owner,
                             file=on_update.file,
                             source_module=on_update.source_module,
                             name=on_update.name, origin=on_update.origin)
        call(sub_env(environ, f"update{count[0]}"), piece)

    return handler


_UPDATE_TAGS = {
    "user_message_chunk": "$UserMessageChunk",
    "agent_message_chunk": "$AgentMessageChunk",
    "agent_thought_chunk": "$AgentThoughtChunk",
}


def the_notification(notification):
    """One `session/update` notification as `SessionNotification`, or None."""
    update = notification.get("update") or {}
    tag = _UPDATE_TAGS.get(update.get("sessionUpdate"))
    if tag is None:
        return None
    piece = viba_ast.ProductChain([
        viba_ast.Tagged("$session_id",
                        viba_ast.Constant(notification.get("sessionId") or "")),
        viba_ast.Tagged("$update", viba_ast.ProductChain([
            viba_ast.Tagged(tag, viba_ast.Nil()),
            viba_ast.Tagged("$content", the_content(update.get("content") or {})),
            viba_ast.Tagged("$message_id",
                            viba_ast.Constant(update["messageId"])
                            if update.get("messageId") else viba_ast.Nil()),
        ])),
    ])
    return viba_data(piece)


def the_content(value):
    """One ACP content block (as JSON) as the interface's `ContentBlock`."""
    if value.get("type") == "text":
        return viba_ast.ProductChain([
            viba_ast.Tagged("$TextContent", viba_ast.Nil()),
            viba_ast.Tagged("$text", viba_ast.Constant(value.get("text") or "")),
            viba_ast.Tagged("$annotations", viba_ast.Nil()),
        ])
    raise RuntimeError(f"content type {value.get('type')!r} is not modeled")


def the_response(conversation, session, blocks, on_update=None):
    """One prompt turn, as the interface's `PromptResponse`."""
    try:
        _answer, stop = conversation.prompt_session(session, blocks, on_update)
    except acp_client.AcpError:
        # The conversation cannot be trusted after a protocol error; drop it so a
        # later call starts a new one.
        _CONVERSATIONS.pop(tuple(AGENT), None)
        conversation.close()
        raise
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$stop_reason", viba_ast.Constant(stop)),
        viba_ast.Tagged("$usage", viba_ast.Nil()),
    ]))


def the_backend():
    """The backend this service process is one instance of."""
    return viba_ast.ProductChain([
        viba_ast.Tagged(the_backend_tag(), viba_ast.Nil()),
        viba_ast.Tagged("$context", viba_ast.Nil()),
    ])


def the_backend_tag():
    """The variant tag naming the agent this service drives."""
    name = the_agent_name()
    try:
        return _BACKEND_TAGS[name]
    except KeyError:
        raise RuntimeError(
            f"unknown agent {name!r}; choose one of "
            f"{', '.join(sorted(_BACKEND_TAGS))}") from None


def the_agent_name():
    """Which ACP agent this service drives."""
    return os.environ.get("VIBA_AGENT", "scripted")


def the_session_value(session):
    """One session, as the interface's `Session`: its backend and its id."""
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$backend", the_backend()),
        viba_ast.Tagged("$session_id", viba_ast.Constant(session)),
    ]))


def the_session_id(session):
    """The backend's session id inside one `Session` argument."""
    backend = session.by_tag("$backend")
    tag = the_backend_tag()
    if not the_has(backend, tag):
        raise RuntimeError(f"this service drives {tag} only")
    return leaf_value(session.by_tag("$session_id"))


def the_trace():
    """Where the ACP messages go, when `run.py` asked for a trace."""
    return os.environ.get("VIBA_AGENT_TRACE")


def main(argv=None) -> int:
    """This service's `__main__`: serve what is owed, or run the program once."""
    return run_service("acp", the_api, argv)


if __name__ == "__main__":
    raise SystemExit(main())
