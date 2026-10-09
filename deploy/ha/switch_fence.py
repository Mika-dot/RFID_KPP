"""IF-MIB port fencing via locally configured Net-SNMP v3 authPriv.

Only dedicated, exclusive reader-access interfaces are supported. Shared host
uplinks/trunks are refused. Credentials stay in a private Net-SNMP config dir;
argv/stdout contain no passwords. --plan invokes neither SNMP nor SQL.
Reference: RFC 2863 section 3.1.13; exit success alone never certifies isolation.
"""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
ADMIN="1.3.6.1.2.1.2.2.1.7."
OPER="1.3.6.1.2.1.2.2.1.8."


def validate(config):
    if config.get("version")!=1 or config.get("dedicated_reader_paths_confirmed") is not True:
        raise ValueError("DedicatedReaderPathsRequired")
    if set(config.get("nodes",{}))!={"physical","perimetr","comparator"}:
        raise ValueError("ThreeFenceMappingsRequired")
    interfaces=set()
    for node in config["nodes"].values():
        ipaddress.ip_address(node["switch_ip"])
        if type(node.get("if_index")) is not int or not 1<=node["if_index"]<=2147483647 or node.get("exclusive") is not True:
            raise ValueError("ExclusiveInterfaceRequired")
        identity=(node["switch_ip"],node["if_index"])
        if identity in interfaces:
            raise ValueError("SharedInterfaceRefused")
        interfaces.add(identity)
    for key in ("snmpget","snmpset","credentials_dir"):
        if not Path(config.get(key,"")).is_absolute():
            raise ValueError("AbsoluteSnmpPathsRequired")
    return config


def credentials(config):
    directory=Path(config["credentials_dir"])
    file=directory/"snmp.conf"
    if directory.is_symlink() or file.is_symlink() or not file.is_file():
        raise RuntimeError("PrivateSnmpCredentialsRequired")
    if os.name!="nt" and (directory.stat().st_mode&0o077 or file.stat().st_mode&0o077 or file.stat().st_uid!=os.geteuid()):
        raise RuntimeError("PrivateSnmpCredentialsRequired")
    text=file.read_text(encoding="utf-8")
    if not re.search(r"(?im)^\s*defSecurityLevel\s+authPriv\s*$",text):
        raise RuntimeError("SnmpAuthPrivRequired")


def snmp(config,node,column,value=None,runner=subprocess.run):
    key="snmpget" if value is None else "snmpset"
    argv=[config[key],"-v","3","-Oqv","-Oe","-t","0.7","-r","0",node["switch_ip"],column+str(node["if_index"])]
    if value is not None:
        argv += ["i",str(value)]
    env={**os.environ,"SNMPCONFPATH":config["credentials_dir"]}
    result=runner(argv,env=env,shell=False,stdin=subprocess.DEVNULL,capture_output=True,timeout=1.1)
    if result.returncode or len(result.stdout)>256:
        raise RuntimeError("SnmpOperationFailed")
    raw=result.stdout.decode("ascii", "strict").strip()
    if not re.fullmatch(r"[1-7]",raw):
        raise RuntimeError("SnmpUnexpectedState")
    return int(raw)


def check_authority(store,node,epoch,allow):
    lease=store.lease()
    if (lease.get("enabled") is not True or lease.get("epoch")!=epoch
            or (allow and (lease.get("owner")!=node or lease.get("valid") is not True))
            or (not allow and lease.get("owner")==node and lease.get("valid") is True)):
        raise RuntimeError("FenceEpochAuthorityChanged")


def operate(config,node_id,epoch,allow,store,runner=subprocess.run):
    validate(config)
    if node_id not in config["nodes"] or type(epoch) is not int or epoch<1:
        raise ValueError("FenceIdentityRequired")
    check_authority(store,node_id,epoch,allow)
    node=config["nodes"][node_id]
    desired=1 if allow else 2
    if snmp(config,node,ADMIN,desired,runner)!=desired:
        raise RuntimeError("FenceSetNotConfirmed")
    admin,oper=snmp(config,node,ADMIN,runner=runner),snmp(config,node,OPER,runner=runner)
    if admin!=desired or (allow and oper!=1) or (not allow and oper not in (2,6)):
        raise RuntimeError("FenceReadbackNotConfirmed")
    check_authority(store,node_id,epoch,allow)
    return {"node":node_id,"reader_access":True,"epoch":epoch} if allow else {"node":node_id,"isolated":True,"epoch":epoch}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",required=True,type=Path)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan",action="store_true");mode.add_argument("--fence",action="store_true");mode.add_argument("--unfence",action="store_true")
    a=p.parse_args(argv)
    cfg=validate(json.loads(a.config.read_text(encoding="utf-8-sig")))
    if a.plan:
        print(json.dumps({"supported":"exclusive_IF_MIB_authPriv","configured_nodes":list(cfg["nodes"]),"changes_applied":False}))
        return 0
    credentials(cfg)
    from guardian.sql import SqlStore
    result=operate(cfg,os.environ["PERIMETER_FENCE_NODE"],int(os.environ["PERIMETER_FENCE_EPOCH"]),a.unfence,SqlStore())
    print(json.dumps(result))
    return 0


if __name__=="__main__":
    try:raise SystemExit(main())
    except Exception as exc:
        print("SWITCH_FENCE_FAILED",type(exc).__name__,file=sys.stderr);raise SystemExit(2)
