import os, sys
import linux
import click
import uuid
import tarfile
import stat


@click.group()
def cli():
    pass

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

def get_container_paths(container_id, container_dir):
    # return rw, workdir, merged
    rw = os.path.join(container_dir, container_id, "rw")
    workdir = os.path.join(container_dir, container_id, "workdir")
    rootfs = os.path.join(container_dir, container_id, "rootfs")
    for each in (rw, workdir, rootfs):
        if not os.path.exists(each):
            os.makedirs(each)
    return rw, workdir, rootfs

def create_container_dir(image_name, image_dir, container_id, container_dir):
    image_root = get_image_root(image_name, image_dir)

    container_rw, container_workdir, container_rootfs = get_container_paths(
        container_id, container_dir
    )

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

def make_pseudofs(new_root):
    # Create pseudo fs /proc, /sys, /dev
    linux.mount('proc', os.path.join(new_root, 'proc'), 'proc', 0, '')
    linux.mount('sysfs', os.path.join(new_root, 'sys'), 'sysfs', 0, '')
    linux.mount('tmpfs', os.path.join(new_root, 'dev'), 'tmpfs',
                linux.MS_NOSUID | linux.MS_STRICTATIME, 'mode=755')

def contain(cmd, container_id, image_name, image_dir, container_dir):
    # create new mount ns
    linux.unshare(linux.CLONE_NEWNS)
    # create new UTS ns
    linux.unshare(linux.CLONE_NEWUTS)
    linux.sethostname(container_id)

    # make new root private recursively
    linux.mount(None, "/", None, linux.MS_PRIVATE | linux.MS_REC, '')

    new_root = create_container_dir(image_name, image_dir,
                                            container_id, container_dir)
    print(f"Created a new root fs for our container: {new_root}")

    make_pseudofs(new_root)
    makedev(new_root)

    old_root = os.path.join(new_root, 'old_root')
    os.makedirs(old_root)
    linux.pivot_root(new_root, old_root)

    os.chdir("/")

    linux.umount2('/old_root', linux.MNT_DETACH)
    os.rmdir('/old_root')

    os.execv(cmd[0], cmd)

    # actually we will never reach here!
    os._exit(0)

@cli.command()
@click.argument('command', required=True, nargs=-1)
@click.option('--image-name', '-i', help="Image Name", default='ubuntu')
@click.option('--image-dir', '-d', help="Image Directory",
              default=os.path.abspath('../images'))
@click.option('--container-dir', '-c', help="Container Directory",
              default=os.path.abspath('../containers'))
def run(command, image_name, image_dir, container_dir):
    container_id = str(uuid.uuid4())
    pid = os.fork()
    if pid == 0:
        # we are in child process
        try:
            contain(command, container_id, image_name, image_dir, container_dir)
        except Exception as e:
            print(f"Child Process Error: {e}")
            sys.exit(1)
    else:
        # we are in father process
        _, status = os.waitpid(pid, 0)
        print(f"{pid} has exited with status {status}")

if __name__ == '__main__':
    cli()
