"""Thin explicit API/subscription reviewer adapters. No backend fallback."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .common import digest, encoded, load, private_directory, sync_directory, write_private
from .transport import https


def anthropic_schema(schema):
    """Native projection only; complete local schema still validates every response."""
    if isinstance(schema, list):
        return [anthropic_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {key: anthropic_schema(value) for key, value in schema.items()
            if key not in {"minimum", "maximum", "minLength", "maxLength"}}


def reviewer_environment(kind, *, parent=None):
    parent = os.environ if parent is None else parent
    # Preserve only operating-system/session auth context, not API keys, proxies,
    # model gateways, instruction overrides or unrelated provider credentials.
    names = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL",
             "SYSTEMROOT", "WINDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    env = {key: value for key, value in parent.items() if key in names}
    if kind == "codex":
        if parent.get("CODEX_HOME"):
            env["CODEX_HOME"] = parent["CODEX_HOME"]
    elif kind == "claude":
        if parent.get("CLAUDE_CONFIG_DIR"):
            env["CLAUDE_CONFIG_DIR"] = parent["CLAUDE_CONFIG_DIR"]
        env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1")
    else:
        raise ValueError("unsupported subscription reviewer")
    return env


def _codex_command(executable, config, schema_path, output_path, instructions_path):
    cmd = [executable, "--no-daemon", "exec", "--ignore-user-config", "--ignore-rules",
           "--strict-config", "--ephemeral", "--skip-git-repo-check", "--json",
           "--sandbox", "read-only", "--model", config["model"],
           "--output-schema", str(schema_path), "--output-last-message", str(output_path)]
    settings = codex_settings(config, instructions_path)
    cmd += [part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))]
    return cmd + ["-"]


def codex_settings(config, instructions_path):
    return {
        "forced_login_method": "chatgpt", "project_doc_max_bytes": 0,
        "project_doc_fallback_filenames": [], "developer_instructions": "",
        "model_instructions_file": str(instructions_path), "approval_policy": "never",
        "web_search": "disabled",
        "features.shell_tool": False, "features.apps": False, "features.plugins": False,
        "features.memories": False, "features.hooks": False, "features.multi_agent": False,
        "features.remote_plugin": False, "features.shell_snapshot": False,
        "features.view_image": False, "features.goals": False,
        "features.computer_use": False, "features.browser_use": False,
        "features.sleep_tool": False, "features.request_permissions_tool": False,
        "features.skip_host_skill_discovery": True,
        "features.default_mode_request_user_input": False,
        "skills.bundled.enabled": False, "skills.include_instructions": False,
        "cli_auth_credentials_store": "file",
        "mcp_servers": {}, "model_reasoning_effort": config["reasoning_effort"],
        "model_provider": "openai", "analytics.enabled": False,
    }


@contextmanager
def isolated_codex_environment(env, work):
    """Use a private auth-only overlay, without copying or reading credential bytes.

    This CLI version loads global AGENTS even with a zero project-doc budget.
    Point only auth.json at its original local file; all instruction/config/skill
    discovery gets an empty HOME/CODEX_HOME. Keyring-only auth fails closed.
    """
    original = (Path(env.get("CODEX_HOME", str(Path(env["HOME"]) / ".codex"))) / "auth.json").resolve()
    if not original.is_file():
        raise ValueError("isolated subscription requires an existing local file-auth login; no credentials are copied")
    if any(original.parent.glob(".news-reviewer-auth-*")):
        raise RuntimeError("stale or active private reviewer auth overlay exists; inspect login/refresh state before continuing")
    home = private_directory(work / "home")
    before = original.stat()
    # Same filesystem permits an atomic move back if the CLI refreshes auth using
    # replace(), which would replace the link rather than its original target.
    overlay = private_directory(tempfile.mkdtemp(prefix=".news-reviewer-auth-", dir=original.parent))
    pointer = overlay / "auth.json"
    pointer.symlink_to(original)
    try:
        yield dict(env, HOME=str(home), CODEX_HOME=str(overlay))
    finally:
        if pointer.exists() and not pointer.is_symlink():
            current = original.stat()
            if (current.st_ino, current.st_mtime_ns, current.st_size) != (before.st_ino, before.st_mtime_ns, before.st_size):
                raise RuntimeError("auth changed concurrently; isolated refresh retained privately for inspection")
            pointer.chmod(0o600)
            os.replace(pointer, original)
            sync_directory(original.parent)
        shutil.rmtree(overlay)


def _claude_command(executable, config, schema_path, instructions_path):
    return [executable, "--print", "--safe-mode", "--tools", "",
            "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--permission-prompts", "none", "--output-format", "stream-json", "--verbose",
            "--model", config["model"], "--effort", config["reasoning_effort"],
            "--system-prompt", instructions_path.read_text(encoding="utf-8"),
            "--json-schema", schema_path.read_text(encoding="utf-8")]


def executable_identity(path):
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size]


def subscription(config, packet, folder, *, parent_env=None, run=subprocess.run, preflight_only=False, preflight=None):
    kind = config["backend"].removesuffix("-subscription")
    if kind not in {"codex", "claude"}:
        raise ValueError("unsupported subscription backend")
    env = reviewer_environment(kind, parent=parent_env)
    executable = shutil.which(config.get("executable", kind), path=env.get("PATH"))
    if not executable:
        return dict(transport_success=False, error="selected reviewer CLI is unavailable", state="unjudged", fatal_configuration=True)
    identity = executable_identity(executable)
    cli_version = None
    if preflight is not None:
        if not preflight.get("transport_success") or preflight.get("configuration_sha256") != digest(config) or preflight.get("executable") != executable or preflight.get("executable_identity") != identity:
            raise ValueError("reviewer executable/configuration changed after preflight")
        cli_version = preflight.get("cli_version")
    else:
        # Cache capability checks for this run only. Auth is still checked per call.
        help_result = run([executable, "exec", "--help"] if kind == "codex" else [executable, "--help"],
                          capture_output=True, text=True, env=env, timeout=30)
        needed = ["--ignore-user-config", "--ignore-rules", "--ephemeral", "--output-schema"] if kind == "codex" else [
            "--safe-mode", "--tools", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
        if help_result.returncode or any(flag not in help_result.stdout for flag in needed):
            return dict(transport_success=False, error="selected CLI cannot enforce required isolation", state="unjudged", fatal_configuration=True)
        if config.get("cli_version"):
            version_result = run([executable, "--version"], capture_output=True, text=True, env=env, timeout=30)
            cli_version = version_result.stdout.strip()
            if version_result.returncode or cli_version != config["cli_version"]:
                return dict(transport_success=False, error="reviewer CLI version differs from prepared package", state="unjudged", fatal_configuration=True)
        if executable_identity(executable) != identity:
            raise ValueError("reviewer executable changed during preflight")
    with ExitStack() as stack, tempfile.TemporaryDirectory(prefix="openweb-news-reviewer-") as temp:
        work = private_directory(temp)
        if kind == "codex":
            env = stack.enter_context(isolated_codex_environment(env, work))
        auth_command = [executable, "--no-daemon", "login", "status"] if kind == "codex" else [executable, "--safe-mode", "auth", "status"]
        auth_result = run(auth_command, capture_output=True, text=True, env=env, timeout=30)
        if kind == "codex":
            auth_ok = auth_result.returncode == 0 and "chatgpt" in (auth_result.stdout + auth_result.stderr).lower()
            auth_observation = "chatgpt" if auth_ok else "unavailable_or_not_subscription"
        else:
            try:
                auth_observation = json.loads(auth_result.stdout).get("authMethod")
            except (ValueError, AttributeError):
                auth_observation = None
            auth_ok = auth_result.returncode == 0 and auth_observation in {"claude.ai", "oauth_token"}
        if not auth_ok:
            return dict(transport_success=False, error="selected subscription auth is unavailable", state="unjudged",
                        auth_observation=auth_observation, fatal_configuration=True)
        if preflight_only:
            return dict(transport_success=True, selected_backend=config["backend"],
                        auth_observation=auth_observation, inference_performed=False,
                        cli_version=cli_version,
                        configuration_sha256=digest(config), executable=executable,
                        executable_identity=identity,
                        required_isolation_flags_present=True)
        schema_path = work / "schema.json"
        output_path = work / "answer.json"
        instructions_path = work / "instructions.md"
        write_private(schema_path, anthropic_schema(packet["schema"]) if kind == "claude" else packet["schema"])
        write_private(instructions_path, packet["prompt"].encode())
        cmd = _codex_command(executable, config, schema_path, output_path, instructions_path) if kind == "codex" else _claude_command(executable, config, schema_path, instructions_path)
        payload = encoded(packet["input"]).decode()
        started = time.monotonic()
        try:
            result = run(cmd, input=payload, cwd=work, env=env, text=True,
                         capture_output=True, timeout=config["timeout_seconds"])
        except subprocess.TimeoutExpired as error:
            def raw(value):
                return value if isinstance(value, bytes) else (value or "").encode()
            write_private(folder / "stdout.jsonl", raw(error.stdout))
            write_private(folder / "stderr.txt", raw(error.stderr))
            return dict(transport_success=False, error="reviewer timed out; partial output retained",
                        dispatch_state="uncertain",
                        selected_backend=config["backend"], account_mode="subscription",
                        auth_observation=auth_observation, requested_model=config["model"],
                        elapsed_seconds=time.monotonic() - started, complete=False)
        # No API credentials exist in child env or request arguments.
        write_private(folder / "stdout.jsonl", result.stdout.encode())
        write_private(folder / "stderr.txt", result.stderr.encode())
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        forbidden, model, verdict, usage, cost_estimate = [], None, None, None, None
        for event in events:
            if event.get("model"):
                model = event["model"]
            if event.get("type") == "system" and event.get("subtype") == "init":
                plugins = event.get("plugins", [])
                builtin_only = all(p.get("name") in {"cc-plugin-agents-md", "cc-plugin-plugin-authoring"} and
                                   p.get("path") == "builtin" and str(p.get("source", "")).endswith("@builtin")
                                   for p in plugins)
                if set(event.get("tools", [])) - {"StructuredOutput"} or event.get("mcp_servers") or event.get("skills") or not builtin_only:
                    forbidden.append("unexpected context or tools in reviewer init")
                if kind == "claude" and (event.get("analytics_disabled") is not True or event.get("product_feedback_disabled") is not True):
                    forbidden.append("unexpected context/privacy mode in reviewer init")
            item = event.get("item", {})
            if item.get("type") and item.get("type") not in {"agent_message", "reasoning"}:
                forbidden.append("unexpected reviewer tool use")
            if event.get("type") == "assistant":
                message = event.get("message", {})
                model = message.get("model", model)
                if any(p.get("type") == "tool_use" and p.get("name") != "StructuredOutput" for p in message.get("content", [])):
                    forbidden.append("unexpected reviewer tool use")
            if event.get("type") == "result":
                verdict = event.get("structured_output")
                if verdict is None and isinstance(event.get("result"), str):
                    try:
                        verdict = json.loads(event["result"])
                    except ValueError:
                        pass
                usage = event.get("usage")
                cost_estimate = event.get("total_cost_usd")
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
        if kind == "codex" and output_path.exists():
            try:
                verdict = load(output_path)
            except ValueError:
                forbidden.append("reviewer final response is not JSON")
        return dict(transport_success=result.returncode == 0 and not forbidden and verdict is not None,
                    fatal_configuration=any("context" in error or "tool use" in error for error in forbidden),
                    dispatch_state="completed" if any(e.get("type") in {"result", "turn.completed"} for e in events) else "uncertain",
                    verdict=verdict, errors=forbidden,
                    exit_code=result.returncode, elapsed_seconds=time.monotonic() - started,
                    selected_backend=config["backend"], account_mode="subscription",
                    auth_observation=auth_observation,
                    cli_version=cli_version,
                    requested_model=config["model"], served_model=model,
                    served_model_observation="reported_by_cli" if model else "not_reported_by_cli",
                    requested_settings=dict(reasoning_effort=config["reasoning_effort"]), usage=usage,
                    cli_cost_usd_estimate=cost_estimate,
                    permitted_context="one blind packet plus CLI system scaffolding; no evidence-access tools, history, skills, memory, MCP or user/repo instructions; attempted interactions fail audit",
                    cli_cost_is_estimate=True, prior_verdict_exposure=False)


def api(config, packet, folder, credentials):
    backend = config["backend"]
    prompt, data, schema = packet["prompt"], encoded(packet["input"]).decode(), packet["schema"]
    if backend == "openai-api":
        request = dict(method="POST", endpoint="https://api.openai.com/v1/responses",
                       body=dict(model=config["model"], instructions=prompt, input=data,
                                 max_output_tokens=config["max_output_tokens"], store=False,
                                 reasoning=dict(effort=config["reasoning_effort"]),
                                 text=dict(format=dict(type="json_schema", name="news_judgment", strict=True, schema=schema))))
        auth = dict(header="Authorization", prefix="Bearer ", secret_ref=config["secret_ref"])
    elif backend == "anthropic-api":
        request = dict(method="POST", endpoint="https://api.anthropic.com/v1/messages",
                       body=dict(model=config["model"], system=prompt,
                                 messages=[dict(role="user", content=data)],
                                 max_tokens=config["max_output_tokens"],
                                 output_config=dict(effort=config["reasoning_effort"],
                                                    format=dict(type="json_schema", schema=anthropic_schema(schema)))))
        auth = dict(header="x-api-key", prefix="", secret_ref=config["secret_ref"])
        # The fixed public API version is protocol, not model identity.
        request["headers"] = {"anthropic-version": "2023-06-01"}
    else:
        raise ValueError("unsupported explicit reviewer API backend")
    write_private(folder / "wire-request.json", request)
    result = https(request, auth, credentials, folder, timeout_seconds=config["timeout_seconds"])
    if not result["transport_success"]:
        return result
    try:
        response = json.loads(result["raw_text"])
        if backend == "openai-api":
            if response.get("status") != "completed":
                raise ValueError("incomplete reviewer response")
            text = "".join(part["text"] for item in response.get("output", []) if item.get("type") == "message"
                           for part in item.get("content", []) if part.get("type") == "output_text")
        else:
            if response.get("stop_reason") != "end_turn":
                raise ValueError("incomplete reviewer response")
            text = "".join(part["text"] for part in response.get("content", []) if part.get("type") == "text")
        return dict(result, verdict=json.loads(text), requested_model=config["model"],
                    served_model=response.get("model"), usage=response.get("usage"),
                    selected_backend=backend, account_mode="api", prior_verdict_exposure=False)
    except (ValueError, TypeError, KeyError):
        return dict(result, transport_success=False, error="invalid or incomplete reviewer API response")
