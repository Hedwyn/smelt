#!/bin/bash
echo "Usage: ../generate_pyconfig_for_host.sh {arch} {python_version} {libc[optional]}"
if [ ! -d ${2} ]; then
    echo "Downloading Python..."
    wget https://www.python.org/ftp/python/3.12.3/Python-3.12.3.tgz
    tar xzf Python-3.12.3.tgz
fi



BUILD=$(uname -m)-linux-gnu
HOST=${1}-linux-${3:-gnu}
echo "Building on ${BUILD} for ${HOST}"
CONFIG_SITE=./config.site ./${2}/configure --host=${HOST} --build=${BUILD} --prefix=${PWD}/python/${2}/build-${1}\
    --disable-ipv6 --with-build-python
