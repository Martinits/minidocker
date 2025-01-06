#!/bin/bash

mount | grep `pwd`/containers | cut -d' ' -f 3 | xargs umount
\rm -rf containers/*
