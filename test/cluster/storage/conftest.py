#!/usr/bin/python3
#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#
import pytest
import pathlib
import shutil
import subprocess
import uuid

from typing import Callable
from contextlib import asynccontextmanager, contextmanager

from test.pylib.manager_client import ManagerClient


@pytest.fixture(scope="function")
def volumes_factory(pytestconfig, build_mode, request):
    @contextmanager
    def wrapper(sizes: list[str]):
        try:
            base = pathlib.Path(f"{pytestconfig.getoption("tmpdir")}/{build_mode}/volumes/{str(uuid.uuid4())}")
            volumes = [base / f"scylla-{id}" for id in range(len(sizes))]
            for path, size in zip(volumes, sizes):
                path.mkdir(parents=True)
                subprocess.run(["sudo", "mount", "-o", f"size={size}", "-t", "tmpfs", "tmpfs", path], check=True)
            yield volumes
        finally:
            for path in volumes:
                subprocess.run(["sudo", "umount", path], check=True)
            shutil.rmtree(base, ignore_errors=False)
    yield wrapper


@asynccontextmanager
async def space_limited_servers(manager: ManagerClient, volumes_factory: Callable, sizes: list[str], **server_args):
    servers = []
    cmdline = server_args.pop("cmdline", [])
    with volumes_factory(sizes) as volumes:
        try:
            servers = [await manager.server_add(cmdline = [*cmdline, '--workdir', str(path)],
                                                property_file={"dc": "dc1", "rack": f"r{id}"},
                                                **server_args) for id, path in enumerate(volumes)]
            yield servers
        finally:
            for server in servers:
                await manager.server_stop(server.server_id)
