import json
import os
import subprocess

DEFAULT_METADATA_FILE = "./metadata.json"

def open_md():
    if not os.path.exists(DEFAULT_METADATA_FILE):
        open(DEFAULT_METADATA_FILE, "w").write('[]')

    with open(DEFAULT_METADATA_FILE, "r") as json_file:
        md = json.load(json_file)

    return md

def close_md(md):
    with open(DEFAULT_METADATA_FILE, "w") as json_file:
        json.dump(md, json_file)

def add_container(cid, pid, nth):
    md = open_md()

    c = {
        'cid': cid,
        'pid': pid,
        'nth': nth,
    }

    md.append(c)

    close_md(md)

def get_container(cid):
    md = open_md()

    c = list(filter(lambda x: x['cid'] == cid, md))

    close_md(md)

    if len(c) == 0:
        return None

    c = c[0]
    c['alive'] = check_pid(c['pid'])

    return c

def check_pid(pid):
    try:
        output = subprocess.check_output(["ps", str(pid)])
        return len(output.split(b"\n")) > 1
    except:
        return False

def list_container():
    md = open_md()

    print("CID\t\t\t\t\tPID\tNth\tStatus")
    for c in md:
        cid = c['cid']
        pid = c['pid']
        nth = c['nth']
        status = "Running" if check_pid(pid) else "Killed"
        print(f"{cid}\t{pid}\t{nth}\t{status}")

    close_md(md)

def del_container(cid):
    md = open_md()

    md[:] = list(filter(lambda x: x['cid'] != cid, md))

    close_md(md)
