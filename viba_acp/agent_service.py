"""The service side of the `viba_acp` interface: the ACP backend's operations.

`new_session` opens a session on the `AcpBackend` the call names and answers its
`Session` (that backend and the session id); `send_prompt` runs one prompt turn on it
and answers the `PromptResponse`. Both go through `service.recorded`, so every call is
recorded under its storage path and a later run on the same store replays it instead of
prompting the agent again.

The conversation — one agent process, and the ACP sessions that process has live — lives
in this process and is closed when the process exits. `Session` is data: it names the
backend and the session the calls use.

Each `AcpBackend` variant is one agent (`claude`, `dsh`); `agents.py` holds each
command and the environment it needs. One service process may drive both.
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

# Which agent each `AcpBackend` variant is; `agents.py` knows its command.
_BACKEND_AGENTS = {
    "$ClaudeAgentBackend": "claude",
    "$DshAgentBackend": "dsh",
}

# One conversation per agent, for the life of this service process.
_CONVERSATIONS = {}


def the_api(service):
    """The interface's table: one function per operation the module declares."""

    def new_session(environ, backend):
        agent = the_backend_agent(backend)
        value = the_backend(backend)
        wiki = the_wiki_prompt(backend)
        return service.recorded(
            environ,
            lambda: the_session_value(
                value, the_conversation(agent).new_session(wiki)))

    def send_prompt(environ, session, prompt_blocks, on_update,
                    sub_env_prefix_on_update):
        agent, session_id = the_session(session)
        blocks = [the_block(block) for block in prompt_blocks]
        handler = the_handler(on_update, environ,
                              leaf_value(sub_env_prefix_on_update))

        def turn():
            conversation = the_conversation(agent)
            conversation.find_session(session_id)
            return the_response(conversation, session_id, blocks, handler)

        return service.recorded(environ, turn)

    def resume_session(environ, backend, session_id, cwd):
        agent = the_backend_agent(backend)
        value = the_backend(backend)
        wiki = the_wiki_prompt(backend)
        session_id = leaf_value(session_id)
        cwd = leaf_value(cwd)

        def resume():
            the_conversation(agent).resume_session(session_id, cwd, wiki)
            return the_session_value(value, session_id)

        return service.recorded(environ, resume)

    def load_session(environ, backend, session_id, cwd, on_update,
                     sub_env_prefix_on_update):
        agent = the_backend_agent(backend)
        value = the_backend(backend)
        wiki = the_wiki_prompt(backend)
        session_id = leaf_value(session_id)
        cwd = leaf_value(cwd)
        handler = the_handler(on_update, environ,
                              leaf_value(sub_env_prefix_on_update))

        def load():
            the_conversation(agent).load_session(session_id, cwd, handler, wiki)
            return the_session_value(value, session_id)

        return service.recorded(environ, load)

    def fork_session(environ, session, cwd):
        agent, parent = the_session(session)
        backend = the_backend(session.by_tag("$backend"))
        cwd = leaf_value(cwd)

        def fork():
            conversation = the_conversation(agent)
            conversation.find_session(parent)
            forked = conversation.fork_session(parent, cwd)
            return the_session_value(backend, forked)

        return service.recorded(environ, fork)

    def on_notification(environ, notification):
        service.recorded(environ, lambda: notification)
        return viba_data(viba_ast.Nil())

    def cancel_session(environ, session):
        agent, session_id = the_session(session)
        the_conversation(agent).cancel_session(session_id)
        return viba_data(viba_ast.Nil())

    def close_session(environ, session):
        agent, session_id = the_session(session)
        the_conversation(agent).close_session(session_id)
        return viba_data(viba_ast.Nil())

    def delete_session(environ, session):
        agent, session_id = the_session(session)
        the_conversation(agent).delete_session(session_id)
        return viba_data(viba_ast.Nil())

    def archive_session(environ, session):
        agent, session_id = the_session(session)
        the_conversation(agent).archive_session(session_id)
        return viba_data(viba_ast.Nil())

    def unarchive_session(environ, session):
        agent, session_id = the_session(session)
        the_conversation(agent).unarchive_session(session_id)
        return viba_data(viba_ast.Nil())

    def set_session_title(environ, session, title):
        agent, session_id = the_session(session)
        the_conversation(agent).set_session_title(session_id, leaf_value(title))
        return viba_data(viba_ast.Nil())

    return {"new_session": new_session, "send_prompt": send_prompt,
            "resume_session": resume_session, "load_session": load_session,
            "fork_session": fork_session,
            "cancel_session": cancel_session, "close_session": close_session,
            "delete_session": delete_session, "archive_session": archive_session,
            "unarchive_session": unarchive_session,
            "set_session_title": set_session_title,
            "on_notification": on_notification}


def the_conversation(agent):
    """The conversation this process has with one agent, made on first use."""
    conversation = _CONVERSATIONS.get(agent)
    if conversation is None:
        command, agent_env = the_command(agent)
        conversation = acp_client.Conversation(
            command, WORKSPACE,
            permissions=os.environ.get("VIBA_AGENT_PERMISSION", "reject"),
            trace=the_trace(), env=agent_env)
        _CONVERSATIONS[agent] = conversation
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


def the_wiki_prompt(backend):
    """The system prompt one backend's session context asks for, or None."""
    context = the_context(backend)
    if context is None:
        return None
    wiki_path = leaf_value(context.by_tag("$wiki_path"))
    return f"The project wiki is at {wiki_path}."


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


def the_handler(on_update, environ, prefix):
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
        call(sub_env(environ, f"{prefix}{count[0]}"), piece)

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
        for agent, known in list(_CONVERSATIONS.items()):
            if known is conversation:
                del _CONVERSATIONS[agent]
                break
        conversation.close()
        raise
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$stop_reason", viba_ast.Constant(stop)),
        viba_ast.Tagged("$usage", viba_ast.Nil()),
    ]))


def the_backend(backend):
    """One `AcpBackend` argument, as a fresh node the store can serialize."""
    members = [viba_ast.Tagged(the_backend_tag(backend), viba_ast.Nil())]
    context = the_context(backend)
    if context is not None:
        members.append(viba_ast.Tagged("$context", the_context_node(context)))
    return viba_ast.ProductChain(members)


def the_backend_tag(backend):
    """The variant tag naming one `AcpBackend` argument."""
    for tag in _BACKEND_AGENTS:
        if the_has(backend, tag):
            return tag
    raise RuntimeError(
        f"unknown ACP backend; this service drives "
        f"{', '.join(sorted(_BACKEND_AGENTS))}")


def the_backend_agent(backend):
    """The agent one `AcpBackend` argument names."""
    return _BACKEND_AGENTS[the_backend_tag(backend)]


def the_context(backend):
    """The `AgentSessionContext` one backend carries, or None."""
    try:
        return backend.by_tag("$context")
    except VibaReflectError:
        return None


def the_context_node(context):
    """One `AgentSessionContext` argument, as a fresh node."""
    version = leaf_value(context.by_tag("$version"))
    return viba_ast.ProductChain([
        viba_ast.Tagged("$wiki_path",
                        viba_ast.Constant(
                            leaf_value(context.by_tag("$wiki_path")))),
        viba_ast.Tagged("$version",
                        viba_ast.Nil() if version is None
                        else viba_ast.Constant(version)),
    ])


def the_session_value(backend, session):
    """One session, as the interface's `Session`: its backend node and its id."""
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$backend", backend),
        viba_ast.Tagged("$session_id", viba_ast.Constant(session)),
    ]))


def the_session(session):
    """The agent and the session id inside one `Session` argument."""
    return (the_backend_agent(session.by_tag("$backend")),
            leaf_value(session.by_tag("$session_id")))


def the_trace():
    """Where the ACP messages go, when `run.py` asked for a trace."""
    return os.environ.get("VIBA_AGENT_TRACE")


def main(argv=None) -> int:
    """This service's `__main__`: serve what is owed, or run the program once."""
    return run_service("acp", the_api, argv)


if __name__ == "__main__":
    raise SystemExit(main())
