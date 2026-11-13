"""Validate and package an allowlisted paper/execution alpha; never publishes it."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION="0.9.7-india-expansion-alpha"
ROOT_FILES={"SKILL.md","README.md","LICENSE","THIRD_PARTY_NOTICES.md","RISK_DISCLOSURE.md","SECURITY.md","CONTRIBUTING.md","CHANGELOG.md",".gitignore",".gitattributes","requirements-dev.txt","requirements-streaming.txt","requirements-streaming-sdk.txt"}
DIR_TYPES={"scripts":{'.py'},"tests":{'.py'},"references":{'.md'},"assets":{'.json'},"agents":{'.yaml'},"third-party":{'.LICENSE'},".github":{'.yml'},".claude-plugin":{'.json'},"skills":{'.md'}}
DENY={"__pycache__","research","test-output","trader",".git",".env",".venv","node_modules"}


def release_files(root=ROOT):
    root=root.resolve();files=[]
    for path in root.rglob("*"):
        rel=path.relative_to(root)
        if any(part in DENY for part in rel.parts) or not path.is_file():continue
        allowed=rel.as_posix() in ROOT_FILES or (len(rel.parts)>1 and path.suffix in DIR_TYPES.get(rel.parts[0],set()))
        if not allowed:continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError("Release symlink/escape refused")
        files.append(path)
    return sorted(files,key=lambda p:p.relative_to(root).as_posix())


def static_checks(root=ROOT):
    files=release_files(root);paths={p.relative_to(root).as_posix() for p in files}
    if not {"SKILL.md","LICENSE","THIRD_PARTY_NOTICES.md","third-party/day-trader-skill.LICENSE"}<=paths:
        raise ValueError("Required skill/license files missing")
    plugin=json.loads((root/".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    marketplace=json.loads((root/".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    if plugin.get("name")!="india-day-trader" or plugin.get("version")!=VERSION or plugin.get("skills")!=["./skills/"]:
        raise ValueError("Plugin identity/version/skills mismatch")
    if marketplace.get("name")!="kunal-india-trading" or marketplace["plugins"][0].get("source")!="./" or marketplace["plugins"][0].get("name")!=plugin["name"]:
        raise ValueError("Marketplace identity/source mismatch")
    expected={"trader-"+n for n in ("scanner","news","entry","exit","timemachine","budget","risk","broker","alerts","analyst","whales")}|{"india-daily","nifty","sensex","banknifty"}
    actual={p.parent.name for p in (root/"skills").glob("*/SKILL.md")}
    if actual!=expected:raise ValueError("Plugin command inventory mismatch")
    for name in expected:
        body=(root/"skills"/name/"SKILL.md").read_text(encoding="utf-8")
        if not body.startswith("---\nname: "+name+"\n") or not re.search(r"^description: .+",body,re.M):raise ValueError("Plugin skill frontmatter invalid")
    links=0
    for path in files:
        content=path.read_text(encoding="utf-8-sig")
        if path.suffix==".py":ast.parse(content,filename=str(path))
        if path.suffix==".json":json.loads(content,parse_constant=lambda x:(_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if path.suffix==".md":
            for href in re.findall(r"\[[^\]]*\]\(([^)]+)\)",content):
                if "://" in href or href.startswith("#"):continue
                target=(path.parent/href.split("#",1)[0]).resolve()
                if not target.is_relative_to(root.resolve()) or target.relative_to(root.resolve()).as_posix() not in paths:
                    raise ValueError("Unpackaged Markdown link: "+str(path.relative_to(root))+" -> "+href)
                links+=1
    skill=(root/"SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\nname: india-day-trader\n") or not re.search(r"^description: .+",skill,re.M):
        raise ValueError("Skill frontmatter missing identity/description")
    return {"version":VERSION,"files":len(files),"local_links":links,"python_syntax":"PASS","json":"PASS",
            "claude_plugin_commands":len(expected),"public_research":True,
            "live_execution_code":True,"live_enabled_by_default":False,"authenticated_execution_tested":False,
            "daily_commands":["/india-daily","/nifty","/sensex","/banknifty"],"portfolio_accounting":"canonical plus five limited broker collectors; reviewed opening and supported provider conventions required",
            "streaming":"five broker tradable quote adapters; authenticated acceptance pending","deployment":"render only; not installed"}


def package(out,root=ROOT):
    checks=static_checks(root)
    files=release_files(root)
    manifest={"version":VERSION,"mode":"paper-and-human-operated-execution-alpha","full_specification_complete":False,
              "daily_commands":["/india-daily","/nifty","/sensex","/banknifty"],"portfolio_accounting":"canonical plus five limited broker collectors; reviewed opening and supported provider conventions required",
              "streaming":"five broker tradable quote adapters; authenticated acceptance pending","deployment":"render only; not installed",
              "claude_plugin_commands":15,"public_research":True,
              "live_enabled_by_default":False,"authenticated_execution_tested":False,
              "authenticated_broker_acceptance":False,"files":{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,"x",compression=zipfile.ZIP_DEFLATED) as archive:
        contents={"india-day-trader/"+p.relative_to(root).as_posix():p.read_bytes() for p in files}
        contents["india-day-trader/RELEASE-MANIFEST.json"]=json.dumps(manifest,sort_keys=True,indent=2).encode()
        for name,body in sorted(contents.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,11,8,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16;archive.writestr(info,body)
    return {**checks,"archive":str(out.resolve()),"sha256":hashlib.sha256(out.read_bytes()).hexdigest()}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--check",action="store_true");p.add_argument("--out")
    a=p.parse_args(argv)
    try:
        if not a.check and not a.out:raise ValueError("Use --check or --out")
        checks=static_checks()
        # There is no skip-tests switch for a distributable archive.
        result=subprocess.run([sys.executable,"-m","unittest","discover","-s",str(ROOT/"tests"),"-q"],check=False,timeout=300)
        if result.returncode:raise ValueError("Release tests failed")
        if a.out:checks=package(a.out)
        print(json.dumps({"status":"PASS","tests":"PASS",**checks},indent=2));return 0
    except (ValueError,OSError,subprocess.TimeoutExpired,zipfile.BadZipFile) as exc:
        print(json.dumps({"status":"BLOCK","reason":str(exc)}));return 2


if __name__=="__main__":sys.exit(main())
