#!/usr/bin/env bash

set -e
set -o pipefail

function install_dependencies() {
  apt-get update >/dev/null
  apt-get -y install python3-dev libsm-dev libxrender1 libxext6 zip git >/dev/null
  rm -rf /var/lib/apt/lists/*

  pip -q install virtualenv
  virtualenv env --python=python3
  . env/bin/activate

  pip -q install -r requirements.txt
}

install_dependencies

python predict.py
