import os, sys
import linux
import click


@click.group()
def cli():
    pass

def contain(cmd):
    os.execv(cmd[0], cmd)
    # actually we will never reach here!
    os._exit(0)

@cli.command()
@click.argument('command', required=True, nargs=-1)
def run(command):
    pid = os.fork()
    if pid == 0:
        # we are in child process
        try:
            contain(command)
        except:
            sys.exit(1)
    else:
        # we are in father process
        _, status = os.waitpid(pid, 0)
        print(f"{pid} has exited with status {status}")

if __name__ == '__main__':
    cli()
