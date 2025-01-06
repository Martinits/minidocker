import os, sys
import linux
import click
import uuid
import tarfile


@click.group()
def cli():
    pass

def get_image_path(image_name, image_dir, image_suffix="tar"):
    return os.path.join(image_dir, image_name+'.'+image_suffix)

def get_container_path(container_id, container_dir):
    return os.path.join(container_dir, container_id, "rootfs")

def create_container_dir(image_name, image_dir, container_id, container_dir):
    image_path = get_image_path(image_name, image_dir)
    assert os.path.exists(image_path), f"Cannot find image {image_path}"

    container_rootfs = get_container_path(container_id, container_dir)

    if not os.path.exists(container_rootfs):
        os.makedirs(container_rootfs)

    with tarfile.open(image_path) as tf:
        # tarfile can contain device files, we don't want them so filter them out
        def nodevs(tarinfo, _):
            return None if tarinfo.type in (tarfile.CHRTYPE, tarfile.BLKTYPE) else tarinfo

        tf.extractall(container_rootfs, filter=nodevs)

    return container_rootfs

def contain(cmd, container_id, image_name, image_dir, container_dir):
    container_rootfs = create_container_dir(image_name, image_dir,
                                            container_id, container_dir)
    print(f"Created a new root fs for our container: {container_rootfs}")

    os.chroot(container_rootfs)
    os.chdir("/")

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
