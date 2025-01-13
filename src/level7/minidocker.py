import os
import linux
import click
import uuid
import tarfile
import stat
import subprocess
import ipaddress
import signal
from metadata import *
import sys
import shutil
import time

IMAGE_BASE_DIR = os.path.abspath('../images')
CONTAINER_BASE_DIR = os.path.abspath('../containers')

VBRIDGE_NAME = "mdbr0"
VBRIDGE_SUBNET_STR = "172.18.0.0/16"
VBRIDGE_SUBNET = ipaddress.IPv4Network(VBRIDGE_SUBNET_STR)
VBRIDGE_SUBNET_BITS = 16
VBRIDGE_SUBNET_GATEWAY = list(VBRIDGE_SUBNET.hosts())[0]

IP_NET_NS_DIR = "/var/run/netns"

CGROUP_BASEDIR = '/sys/fs/cgroup'
CGROUP_DIR = os.path.join(CGROUP_BASEDIR, 'minidocker')


def handle_signal(signum, frame):
    pass

def get_cidr(ipaddr):
    return f"{ipaddr}/{VBRIDGE_SUBNET_BITS}"

@click.group()
def main():
    signal.signal(signal.SIGUSR1, handle_signal)
    # create vbridge
    output = subprocess.check_output(['brctl', 'show'], text=True)
    if VBRIDGE_NAME not in output:
        run_cmd(f"brctl addbr {VBRIDGE_NAME}")
        run_cmd(f"ip addr add {get_cidr(VBRIDGE_SUBNET_GATEWAY)} dev {VBRIDGE_NAME}")
    run_cmd(f"ip link set {VBRIDGE_NAME} up")

    # add forward rules
    run_cmd("sysctl net.ipv4.conf.all.forwarding=1")
    output = subprocess.check_output(['iptables', '-t', 'nat', '-L'], text=True)
    if VBRIDGE_SUBNET_STR not in output:
        run_cmd(f"iptables -t nat -A POSTROUTING -s {VBRIDGE_SUBNET_STR} ! -o {VBRIDGE_NAME} -j MASQUERADE")

    # create minidocker cpu cgroup
    os.makedirs(CGROUP_DIR, exist_ok=True)
    md_cg_subtree = os.path.join(CGROUP_DIR, 'cgroup.subtree_control')
    open(md_cg_subtree, 'w').write('+cpu +memory')

def get_image_root(image_name, image_dir, image_suffix="tar"):
    image_root = os.path.join(image_dir, image_name)
    if os.path.exists(image_root):
        return image_root

    # extract tarball if image_root does not exist
    os.makedirs(image_root)

    image_path = os.path.join(image_dir, image_name+'.'+image_suffix)
    assert os.path.exists(image_path), f"Cannot find image {image_path}"

    with tarfile.open(image_path) as tf:
        # tarfile can contain device files, we don't want them so filter them out
        def nodevs(tarinfo, _):
            return None if tarinfo.type in (tarfile.CHRTYPE, tarfile.BLKTYPE) else tarinfo

        tf.extractall(image_root, filter=nodevs)

    return image_root

def get_container_paths(container_id):
    # return rw, workdir, merged
    rw = os.path.join(CONTAINER_BASE_DIR, container_id, "rw")
    workdir = os.path.join(CONTAINER_BASE_DIR, container_id, "workdir")
    rootfs = os.path.join(CONTAINER_BASE_DIR, container_id, "rootfs")
    for each in (rw, workdir, rootfs):
        if not os.path.exists(each):
            os.makedirs(each)
    return rw, workdir, rootfs

def create_container_dir(image_name, container_id):
    image_root = get_image_root(image_name, IMAGE_BASE_DIR)

    container_rw, container_workdir, container_rootfs = get_container_paths(container_id)

    linux.mount(
        'overlay', container_rootfs, 'overlay', linux.MS_NODEV,
        f"lowerdir={image_root},upperdir={container_rw},workdir={container_workdir}")

    return container_rootfs

def makedev(new_root):
    # Add some basic devices
    dev_path = os.path.join(new_root, 'dev')
    devpts_path = os.path.join(dev_path, 'pts')
    if not os.path.exists(devpts_path):
        os.makedirs(devpts_path)
        linux.mount('devpts', devpts_path, 'devpts', 0, '')
    for i, dev in enumerate(['stdin', 'stdout', 'stderr']):
        os.symlink('/proc/self/fd/%d' % i, os.path.join(new_root, 'dev', dev))

    old_umask = os.umask(0)

    # create /dev/null
    os.mknod(os.path.join(dev_path, "null"), 0o666 | stat.S_IFCHR, os.makedev(1, 3))
    # create /dev/zero
    os.mknod(os.path.join(dev_path, "zero"), 0o666 | stat.S_IFCHR, os.makedev(1, 5))
    #crete /dev/full
    os.mknod(os.path.join(dev_path, "full"), 0o666 | stat.S_IFCHR, os.makedev(1, 7))
    # create /dev/random
    os.mknod(os.path.join(dev_path, "random"), 0o666 | stat.S_IFCHR, os.makedev(1, 8))
    # create /dev/urandom
    os.mknod(os.path.join(dev_path, "urandom"), 0o666 | stat.S_IFCHR, os.makedev(1, 9))
    #crete /dev/console
    os.mknod(os.path.join(dev_path, "console"), 0o666 | stat.S_IFCHR, os.makedev(136, 1))
    #crete /dev/tty
    os.mknod(os.path.join(dev_path, "tty"), 0o666 | stat.S_IFCHR, os.makedev(5, 0))

    os.umask(old_umask)

def make_pseudofs(new_root):
    # Create pseudo fs /proc, /sys, /dev
    linux.mount('proc', os.path.join(new_root, 'proc'), 'proc', 0, '')
    linux.mount('sysfs', os.path.join(new_root, 'sys'), 'sysfs', 0, '')
    linux.mount('tmpfs', os.path.join(new_root, 'dev'), 'tmpfs',
                linux.MS_NOSUID | linux.MS_STRICTATIME, 'mode=755')

def container_setup_vnet(ipaddr, gateway, veth):
    # set ip addr
    run_cmd(f"ip addr add {get_cidr(ipaddr)} dev {veth}")
    run_cmd(f"ip link set {veth} up")
    # create routing inside ns
    run_cmd(f"ip route add default via {gateway} dev {veth}")

def setup_cgroup(container_id, pid, cpu_shares, mlimit, mslimit):
    print("Setting cgroup...")
    cg_dir = os.path.join(CGROUP_DIR, container_id)

    # Insert the container to new cgroup
    if not os.path.exists(cg_dir):
        os.makedirs(cg_dir)
    proc_file = os.path.join(cg_dir, 'cgroup.procs')
    open(proc_file, 'w').write(str(pid))

    # cpu cgroup
    if cpu_shares:
        cpu_shares_file = os.path.join(cg_dir, 'cpu.weight')
        open(cpu_shares_file, 'w').write(str(cpu_shares))

    # memory cgroup
    if mlimit:
        mmax_file = os.path.join(cg_dir, 'memory.max')
        open(mmax_file, 'w').write(str(mlimit))
    if mslimit:
        msmax_file = os.path.join(cg_dir, 'memory.swap.max')
        open(msmax_file, 'w').write(str(mslimit))

def clean_cgroup(container_id):
    print("Host cleaning cgroup...")
    cg_dir = os.path.join(CGROUP_DIR, container_id)
    if os.path.exists(cg_dir):
        os.rmdir(cg_dir)

def contain(cmd, container_id, rootfs, ipaddr, gateway, veth, detach):
    if detach:
        print("Redirecting stdin, stdout, stderr...")
        fd_in = os.open("./stdin", os.O_RDONLY)
        os.dup2(fd_in, sys.stdin.fileno())
        fd_out = os.open("./stdout", os.O_WRONLY)
        os.dup2(fd_out, sys.stdout.fileno())
        os.dup2(fd_out, sys.stderr.fileno())

    print("Waiting for host signal...")
    signal.pause()
    print("Got host signal, continue...")

    container_setup_vnet(ipaddr, gateway, veth)

    linux.sethostname(container_id)

    # make new root private recursively
    linux.mount(None, "/", None, linux.MS_PRIVATE | linux.MS_REC, '')

    make_pseudofs(rootfs)
    makedev(rootfs)

    old_root = os.path.join(rootfs, 'old_root')
    os.makedirs(old_root)
    linux.pivot_root(rootfs, old_root)

    os.chdir("/")

    linux.umount2('/old_root', linux.MNT_DETACH)
    os.rmdir('/old_root')

    os.execv(cmd[0], cmd)

    # actually we will never reach here!
    os._exit(0)

# maybe called multiple times
def nth_container():
    # assuming we launch only one container for now
    return 1

def get_next_vnet_ip(nth):
    return list(VBRIDGE_SUBNET.hosts())[nth]

def veth_pair_name(nth):
    veth_inside = f"veth{nth}_0"
    veth_outside = f"veth{nth}_1"
    return veth_inside, veth_outside


def run_cmd(cmd):
    # print(f"Running {cmd}")
    subprocess.run(cmd.split(), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def create_vnet(pid, nth):
    print("Host creating vnet...")
    ns = f"mdns{pid}"
    ns_path = os.path.join(IP_NET_NS_DIR, ns)

    # link net ns
    os.makedirs(IP_NET_NS_DIR, exist_ok=True)
    run_cmd(f"ln -sf /proc/{pid}/ns/net {ns_path}")

    veth_inside, veth_outside = veth_pair_name(nth)
    # create veth pair
    run_cmd(f"ip link add {veth_inside} type veth peer name {veth_outside}")
    run_cmd(f"ip link set {veth_inside} netns {ns}")

    # connect to vbridge
    run_cmd(f"brctl addif {VBRIDGE_NAME} {veth_outside}")
    run_cmd(f"ip link set {veth_outside} up")

def clean_vnet(pid):
    print("Host cleaning vnet...")
    ns = f"mdns{pid}"
    ns_path = f"/var/run/netns/{ns}"
    os.remove(ns_path)

def clean_mount(cid):
    _, _, rootfs = get_container_paths(cid)
    linux.umount(rootfs)
    shutil.rmtree(os.path.dirname(rootfs))

def do_clean(cid, pid):
    clean_vnet(pid)
    clean_cgroup(cid)
    clean_mount(cid)
    del_container(cid)

def wait_pid(pid):
    print(f"Waiting for {pid}...")
    _, status = os.waitpid(pid, 0)
    print(f"{pid} has exited with status {status}")

@main.command()
@click.argument('command', required=True, nargs=-1)
@click.option('--image-name', '-i', help="Image Name", default='ubuntu')
@click.option('--cpu-shares', help='CPU Shares (relative weight)', default=0)
@click.option('--mlimit', help='Memory limit', default=None)
@click.option('--mslimit', help='Memory(swap) limit', default=None)
@click.option('--daemon', '-d', help='Run as daemon', is_flag=True)
def run(command, image_name, cpu_shares, mlimit, mslimit, daemon):
    container_id = str(uuid.uuid4())

    nth = nth_container()
    ipaddr = get_next_vnet_ip(nth)
    gateway = VBRIDGE_SUBNET_GATEWAY
    veth, _ = veth_pair_name(nth)

    if not os.path.exists("./stdin"):
        os.mkfifo("./stdin")
    if not os.path.exists("./stdout"):
        os.mkfifo("./stdout")

    rootfs = create_container_dir(image_name, container_id)
    print(f"Mount a new root fs for our container: {rootfs}")

    print("Host cloning...")
    flags = linux.CLONE_NEWPID | linux.CLONE_NEWNS | linux.CLONE_NEWUTS | linux.CLONE_NEWNET
    cb_args = (command, container_id, rootfs, ipaddr, gateway, veth, daemon)
    pid = linux.clone(contain, flags, cb_args)

    # here is father process
    add_container(container_id, pid, nth)
    create_vnet(pid, nth)
    setup_cgroup(container_id, pid, cpu_shares, mlimit, mslimit)

    print("Host resuming child...")
    os.kill(pid, signal.SIGUSR1)

    if daemon:
        print(f"Detach {pid}")
        return

    wait_pid(pid)

    do_clean(container_id, pid)

def container_exec(cmd, cid, pid):
    # enter namespaces
    print("Entering namespaces...")
    ns_list = ['net', 'mnt', 'uts', 'pid']
    ns_flag = [linux.CLONE_NEWNET, linux.CLONE_NEWNS, linux.CLONE_NEWUTS, linux.CLONE_NEWPID]
    for ns, flag in zip(ns_list, ns_flag):
        ns_path = f"/proc/{pid}/ns/{ns}"
        fd = os.open(ns_path, os.O_RDONLY)
        os.setns(fd, flag)
        os.close(fd)

    # enter cgroup
    cg_dir = os.path.join(CGROUP_DIR, cid)
    if os.path.exists(cg_dir):
        print("Entering CGroup...")
        proc_file = os.path.join(cg_dir, 'cgroup.procs')
        open(proc_file, 'w').write(str(os.getpid()))

    linux.sethostname(cid)

    _, _, rootfs = get_container_paths(cid)

    # make new root private recursively
    linux.mount(None, "/", None, linux.MS_PRIVATE | linux.MS_REC, '')

    make_pseudofs(rootfs)
    makedev(rootfs)

    # pivot_root
    old_root = os.path.join(rootfs, 'old_root')
    if not os.path.exists(old_root):
        os.makedirs(old_root)
    linux.pivot_root(rootfs, old_root)
    os.chdir("/")

    linux.umount2('/old_root', linux.MNT_DETACH)
    os.rmdir('/old_root')

    os.execv(cmd[0], cmd)

    # actually we will never reach here!
    os._exit(0)

@main.command()
@click.argument('command', required=True, nargs=-1)
@click.option('--container', '-c', help='Container ID', required=True)
def exec(command, container):
    c = get_container(container)
    if c is None:
        print(f"{container} does not exist!")
        return

    cid = c['cid']
    pid = c['pid']

    print("Host exec cloning...")
    cb_args = (command, cid, pid)
    pid = linux.clone(container_exec, 0, cb_args)

    wait_pid(pid)

@main.command()
def ps():
    list_container()

@main.command()
@click.argument('container_id', required=True, nargs=1)
def stop(container_id):
    c = get_container(container_id)
    if c is None:
        print(f"{container_id} does not exist!")
        return

    cid = c['cid']
    pid = c['pid']

    if c['alive']:
        print(f"Sending SIGKILL to {pid}")
        os.kill(pid, signal.SIGKILL)

    while check_pid(pid):
        time.sleep(0.1)

    do_clean(cid, pid)

# @main.result_callback()
# def clean(result, **kwargs):
#     print("Do global cleaning...")
#     # delete vbridge
#     run_cmd(f"ip link set {VBRIDGE_NAME} down")
#     run_cmd(f"brctl delbr {VBRIDGE_NAME}")
#
#     # remove forward rules
#     run_cmd(f"iptables -t nat -D POSTROUTING -s {VBRIDGE_SUBNET_STR} ! -o {VBRIDGE_NAME} -j MASQUERADE")

if __name__ == '__main__':
    main()
