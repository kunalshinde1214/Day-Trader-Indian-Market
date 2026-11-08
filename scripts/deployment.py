"""Render reviewable per-user monitoring deployment files. Never installs or starts them."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from core import Block, integer
from trader import read

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def path(value, target):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value) or '"' in value:
        raise Block("DEPLOY: invalid path characters")
    cls = PureWindowsPath if target == "windows" else PurePosixPath
    result = cls(value)
    if not result.is_absolute() or ".." in result.parts or target == "windows" and str(result.drive).startswith("\\\\"):
        raise Block("DEPLOY: absolute local paths without traversal required")
    return str(result)


def unit_arg(value):
    # systemd has its own specifier/environment expansion, even without a shell.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render(spec):
    if spec.get("schema") != "readonly_deployment_v1" or spec.get("target") not in {"windows", "systemd"}:
        raise Block("DEPLOY: supported deployment schema/target required")
    target = spec["target"]; name = spec["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,60}", name): raise Block("DEPLOY: simple lowercase task name required")
    executable, skill, work = (path(spec[k], target) for k in ("python", "skill_dir", "workdir"))
    cls = PureWindowsPath if target == "windows" else PurePosixPath
    component = spec["component"]
    if component == "monitor":
        cycles, interval, failures = (integer(spec[k]) for k in ("cycles", "interval", "max_failures"))
        if cycles > 8640 or not 5 <= interval <= 60 or failures > 20 or type(spec["broker_poll"]) is not bool:
            raise Block("DEPLOY: bounded monitoring settings required")
        args = [str(cls(skill) / "scripts/service.py"), "run", "--workdir", work, "--cycles", str(cycles), "--interval", str(interval), "--max-failures", str(failures)]
        if spec["broker_poll"]: args.append("--broker-poll")
        stop = [executable, str(cls(skill) / "scripts/service.py"), "stop", "--workdir", work]
    elif component == "stream":
        duration, retries = integer(spec["duration"]), integer(spec["retries"], minimum=0)
        if duration > 43200 or retries > 5: raise Block("DEPLOY: bounded streaming settings required")
        args = [str(cls(skill) / "scripts/streaming.py"), "run", "--workdir", work, "--master", path(spec["master"], target), "--duration", str(duration), "--retries", str(retries)]
        stop = [executable, str(cls(skill) / "scripts/streaming.py"), "stop", "--workdir", work]
    else: raise Block("DEPLOY: only read-only monitor/stream components can be deployed")
    files = {}
    if target == "windows":
        if spec.get("environment_file"): raise Block("DEPLOY: Windows task needs an explicitly configured user environment")
        user = spec["windows_user"]
        if not isinstance(user, str) or not user.strip() or any(ord(c) < 32 for c in user): raise Block("DEPLOY: interactive Windows user required")
        ET.register_namespace("", NS)
        task = ET.Element("{" + NS + "}Task", version="1.2")
        def child(parent, tag, value=None, **attrs):
            item = ET.SubElement(parent, "{" + NS + "}" + tag, attrs)
            if value is not None: item.text = str(value)
            return item
        registration = child(task, "RegistrationInfo"); child(registration, "Description", "Read-only India Day Trader " + component)
        trigger = child(child(task, "Triggers"), "LogonTrigger"); child(trigger, "Enabled", "true"); child(trigger, "UserId", user)
        principal = child(child(task, "Principals"), "Principal", id="Author")
        child(principal, "UserId", user); child(principal, "LogonType", "InteractiveToken"); child(principal, "RunLevel", "LeastPrivilege")
        settings = child(task, "Settings")
        for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false"),
                           ("StartWhenAvailable", "false"), ("Enabled", "true"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S")):
            child(settings, tag, value)
        action = child(child(task, "Actions", Context="Author"), "Exec")
        child(action, "Command", executable); child(action, "Arguments", subprocess.list2cmdline(args)); child(action, "WorkingDirectory", work)
        files[name + ".xml"] = ET.tostring(task, encoding="unicode", xml_declaration=False)
        instructions = {"register": ["schtasks", "/Create", "/TN", name, "/XML", name + ".xml"],
                        "start": ["schtasks", "/Run", "/TN", name], "unregister": ["schtasks", "/Delete", "/TN", name], "stop_latch": stop}
    else:
        lines = ["[Unit]", "Description=Read-only India Day Trader " + component, "", "[Service]", "Type=simple",
                 "WorkingDirectory=" + unit_arg(work).replace("$$", "$"), "ExecStart=" + " ".join(unit_arg(v) for v in [executable, *args]),
                 "ExecStop=" + " ".join(unit_arg(v) for v in stop), "Restart=no", "UMask=0077", "NoNewPrivileges=true", "TimeoutStopSec=30"]
        if spec.get("environment_file"): lines.append("EnvironmentFile=" + unit_arg(path(spec["environment_file"], target)).replace("$$", "$"))
        lines += ["", "[Install]", "WantedBy=default.target", ""]
        files[name + ".service"] = "\n".join(lines)
        instructions = {"copy_to": "~/.config/systemd/user/" + name + ".service", "reload": ["systemctl", "--user", "daemon-reload"],
                        "enable": ["systemctl", "--user", "enable", name + ".service"], "start": ["systemctl", "--user", "start", name + ".service"],
                        "stop": ["systemctl", "--user", "stop", name + ".service"], "disable": ["systemctl", "--user", "disable", name + ".service"], "stop_latch": stop}
    files["operator-actions.json"] = json.dumps({"target": target, "component": component, "commands": instructions,
        "installed": False, "started": False, "execution_enabled": False,
        "notes": ["Run commands only after reviewing paths and the local user/service environment.",
                  "No automatic restart after failure, stop or bounded completion. Stop latches must be explicitly cleared.",
                  "The daily master, credentials and sourced context must be refreshed independently."]}, indent=2)
    return files


def save(spec, directory):
    files = render(spec); out = Path(directory)
    if out.exists(): raise Block("DEPLOY: choose a new artifact directory")
    out.mkdir(parents=True)
    for name, body in files.items(): (out / name).write_bytes(body.encode("utf-8"))
    manifest = {"schema": "deployment_manifest_v1", "files": {name: hashlib.sha256(body.encode()).hexdigest() for name, body in files.items()}, "installed": False}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"rendered": list(files), "directory": str(out.resolve()), "installed": False, "started": False}


def verify(directory):
    out = Path(directory).resolve(); manifest = read(out / "manifest.json")
    if manifest.get("schema") != "deployment_manifest_v1" or not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise Block("DEPLOY: manifest required")
    for name, digest in manifest["files"].items():
        target = out / name
        if Path(name).name != name or not target.resolve().is_relative_to(out) or target.is_symlink(): raise Block("DEPLOY: invalid artifact path")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest: raise Block("DEPLOY: artifact changed")
    return {"status": "PASS", "files_verified": len(manifest["files"]), "installed": False, "meaning": "integrity check, not OS deployment acceptance"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["render", "verify"]); p.add_argument("--spec"); p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        result = save(read(a.spec), a.out) if a.command == "render" else verify(a.out)
        print(json.dumps(result)); return 0
    except (Block, ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "BLOCK", "reason": "Deployment artifact validation failed", "installed": False})); return 2


if __name__ == "__main__": sys.exit(main())
