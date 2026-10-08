"""Plan/install the independent Web gateway and Behavior Observer on Linux.

Does not install Guardian, migrate SQL, change a lease or manufacture business
events. --plan is read-only. --install requires a clean, exact checkout and a
private, operator-provisioned environment file. Failed service activation restores
previous units. Factory use belongs to the subsequent installation stage.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

SERVICES={"gateway":("gateway.server",5051),"behavior":("observer.service",19153)}


def run(argv, check=True):
    return subprocess.run(argv,check=check,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)


def unit(root,python,config,env_file,kind,user="perimeter-observer"):
    for path in (root,python,config,env_file):
        if not Path(path).is_absolute() or re.search(r"[\s%\"'\\]",str(path)):
            raise ValueError("SimpleAbsoluteDeploymentPathsRequired")
    return "\n".join(("[Unit]","Description=Perimeter "+kind,"After=network-online.target","Wants=network-online.target",
        "[Service]","Type=simple","User="+user,"Group="+user,"WorkingDirectory="+str(root),
        "EnvironmentFile="+str(env_file),"Environment=PYTHONUTF8=1","ExecStart="+str(python)+" -m "+SERVICES[kind][0]+" --config "+str(config),
        "Restart=always","RestartSec=5","TimeoutStopSec=15","NoNewPrivileges=true","PrivateTmp=true","UMask=0077",
        "[Install]","WantedBy=multi-user.target",""))


def ready(port):
    try:
        with urlopen("http://127.0.0.1:"+str(port)+"/health/ready",timeout=4) as response:
            return response.status==200
    except (OSError,HTTPError):
        return False


def install(root,python,configs,env_file,release):
    if sys.platform!="linux" or os.geteuid()!=0:
        raise RuntimeError("LinuxRootRequired")
    import pwd
    if env_file.is_symlink() or not env_file.is_file() or env_file.stat().st_uid!=0 or env_file.stat().st_mode & 0o077:
        raise RuntimeError("PrivateRootEnvironmentFileRequired")
    if run(["git","-C",str(root),"rev-parse","HEAD"]).stdout.strip()!=release or run(["git","-C",str(root),"status","--porcelain"]).stdout.strip():
        raise RuntimeError("CleanExactReleaseRequired")
    ports={}
    for kind,config in configs.items():
        if config.is_symlink() or not config.is_file():
            raise RuntimeError("DeploymentConfigRequired")
        value=json.loads(config.read_text(encoding="utf-8-sig"))
        ports[kind]=value.get("port",SERVICES[kind][1])
        if type(ports[kind]) is not int or not 1<=ports[kind]<=65535:
            raise ValueError("InvalidServicePort")
    username="perimeter-observer"
    try:account=pwd.getpwnam(username)
    except KeyError:
        run(["useradd","--system","--home-dir","/var/lib/perimeter-behavior","--shell","/usr/sbin/nologin",username]);account=pwd.getpwnam(username)
    state=Path("/var/lib/perimeter-behavior");state.mkdir(parents=True,exist_ok=True);os.chown(state,account.pw_uid,account.pw_gid);state.chmod(0o700)
    backup=Path("/var/lib/perimeter-resilience-install")/str(time.time_ns());backup.mkdir(parents=True,mode=0o700)
    saved={}
    for kind in configs:
        name="perimeter-"+("gateway" if kind=="gateway" else "behavior")+".service"
        target=Path("/etc/systemd/system")/name
        if target.is_symlink():raise RuntimeError("ExistingUnitSymlinkRefused")
        saved[kind]={"name":name,"path":target,"data":target.read_bytes() if target.exists() else None,
                     "enabled":run(["systemctl","is-enabled",name],False).returncode==0,
                     "active":run(["systemctl","is-active",name],False).returncode==0}
        if target.exists():shutil.copyfile(target,backup/name)
    try:
        for kind,config in configs.items():saved[kind]["path"].write_text(unit(root,python,config,env_file,kind),encoding="utf-8")
        run(["systemctl","daemon-reload"])
        for kind,entry in saved.items():
            run(["systemctl","enable",entry["name"]]);run(["systemctl","restart",entry["name"]])
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if all(ready(ports[k]) for k in configs):
                return {"services_ready":list(configs),"release":release}
            time.sleep(1)
        raise RuntimeError("IndependentServicesNotReady")
    except Exception:
        for entry in saved.values():
            run(["systemctl","stop",entry["name"]],False)
            if entry["data"] is None:
                run(["systemctl","disable",entry["name"]],False);entry["path"].unlink(missing_ok=True)
            else:entry["path"].write_bytes(entry["data"])
        run(["systemctl","daemon-reload"],False)
        for entry in saved.values():
            run(["systemctl","enable" if entry["enabled"] else "disable",entry["name"]],False)
            if entry["active"]:run(["systemctl","start",entry["name"]],False)
        raise


def main(argv=None):
    p=argparse.ArgumentParser();mode=p.add_mutually_exclusive_group(required=True);mode.add_argument("--plan",action="store_true");mode.add_argument("--install",action="store_true")
    p.add_argument("--root",required=True,type=Path);p.add_argument("--python",required=True,type=Path)
    p.add_argument("--gateway-config",type=Path);p.add_argument("--behavior-config",type=Path)
    p.add_argument("--env-file",required=True,type=Path);p.add_argument("--release",required=True)
    a=p.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}",a.release):raise ValueError("ExactReleaseRequired")
    configs={k:v.resolve() for k,v in (("gateway",a.gateway_config),("behavior",a.behavior_config)) if v}
    if not configs:raise ValueError("AtLeastOneServiceRequired")
    if a.plan:
        for k,v in configs.items():unit(a.root.resolve(),a.python.resolve(),v,a.env_file.resolve(),k)
        print(json.dumps({"mode":"plan","release":a.release,"services":list(configs),"changes_applied":False}))
        return 0
    print(json.dumps(install(a.root.resolve(),a.python.resolve(),configs,a.env_file.resolve(),a.release)))
    return 0


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as exc:print("RESILIENCE_INSTALL_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
