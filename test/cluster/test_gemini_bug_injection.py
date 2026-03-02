#
# Copyright (C) 2025-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.1
#

import logging
import pytest
import time
from typing import Any

from cassandra.cluster import ConsistencyLevel, Session  # type: ignore
from cassandra.query import SimpleStatement  # type: ignore
from cassandra.pool import Host  # type: ignore

from test.pylib.util import wait_for_cql_and_get_hosts, execute_with_tracing
from test.pylib.manager_client import ManagerClient
from test.cluster.util import new_test_keyspace


logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_read_repair_gemini_bug_drop_write_keeps_inconsistency(request: pytest.FixtureRequest, manager: ManagerClient) -> None:
    """Test that gemini_bug_every_n injection causes read-repair writes to be
    silently dropped, keeping the cluster inconsistent across repeated reads."""
    cmdline = ["--hinted-handoff-enabled", "0", "--logger-log-level", "mutation_data=trace:debug_error_injection=trace"]
    config = {"read_request_timeout_in_ms": 60000}
    node1, node2 = await manager.servers_add(2, cmdline=cmdline, config=config, auto_rack_dc="dc1")

    cql = manager.get_cql()
    await wait_for_cql_and_get_hosts(cql, [node1, node2], time.time() + 60)

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 2};") as ks:
        await cql.run_async(f"CREATE TABLE {ks}.t (pk bigint PRIMARY KEY, c int)")

        await manager.server_stop_gracefully(node2.server_id)
        await manager.driver_connect(node1)
        cql = manager.get_cql()

        insert_stmt = cql.prepare(f"INSERT INTO {ks}.t (pk, c) VALUES (?, ?)")
        insert_stmt.consistency_level = ConsistencyLevel.ONE
        await cql.run_async(insert_stmt, (1, 111))

        await manager.server_start(node2.server_id, wait_others=1)
        await manager.driver_connect()
        cql, hosts = await manager.get_ready_cql([node1, node2])
        host1, _ = hosts

        def has_read_repair(tracing_events: list[list[Any]]) -> bool:
            return any(event.description == "digest mismatch, starting read repair" for page in tracing_events for event in page)

        select_stmt = SimpleStatement(f"SELECT * FROM {ks}.t WHERE pk = 1", consistency_level=ConsistencyLevel.ALL)

        await manager.api.enable_injection(node1.ip_addr, "gemini_bug_every_n", one_shot=False, parameters={"value": "1"})
        await manager.api.enable_injection(node2.ip_addr, "gemini_bug_every_n", one_shot=False, parameters={"value": "1"})
        try:
            tracing = execute_with_tracing(cql, select_stmt, host=host1, log=True)
            assert has_read_repair(tracing)

            tracing = execute_with_tracing(cql, select_stmt, host=host1, log=True)
            assert has_read_repair(tracing)
        finally:
            await manager.api.disable_injection(node1.ip_addr, "gemini_bug_every_n")
            await manager.api.disable_injection(node2.ip_addr, "gemini_bug_every_n")

        # After disabling injection, one read-repair fixes it, then it stays consistent.
        tracing = execute_with_tracing(cql, select_stmt, host=host1, log=True)
        assert has_read_repair(tracing)

        tracing = execute_with_tracing(cql, select_stmt, host=host1, log=True)
        assert not has_read_repair(tracing)


@pytest.mark.asyncio
@pytest.mark.skip_mode(mode='release', reason='error injections are not supported in release mode')
async def test_repair_gemini_bug_keeps_inconsistency(request: pytest.FixtureRequest, manager: ManagerClient) -> None:
    """Test that gemini_bug_every_n injection during row-level repair causes
    repair to silently drop rows/batches on the follower, leaving inconsistency.

    The injection fires on the follower node when the repair master pushes rows
    to it via apply_mutation_from_repair_master / do_apply_rows.
    We write data while node1 is down, so node1 is the follower missing data
    for tablets where node2 is the repair master."""
    cmdline = ["--hinted-handoff-enabled", "0", "--logger-log-level", "repair=debug:debug_error_injection=trace"]
    node1, node2 = await manager.servers_add(2, cmdline=cmdline, auto_rack_dc="dc1")

    cql = manager.get_cql()
    await wait_for_cql_and_get_hosts(cql, [node1, node2], time.time() + 60)

    async with new_test_keyspace(manager, "WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 2};") as ks:
        await cql.run_async(f"CREATE TABLE {ks}.t (pk int, ck int, v int, PRIMARY KEY (pk, ck))")

        # Create inconsistency: stop node1, write to node2 only
        await manager.server_stop_gracefully(node1.server_id)
        await manager.driver_connect(node2)
        cql = manager.get_cql()

        # Use many partition keys to spread across tablets
        num_pks = 50
        num_cks = 4
        total_rows = num_pks * num_cks
        insert_stmt = cql.prepare(f"INSERT INTO {ks}.t (pk, ck, v) VALUES (?, ?, ?)")
        insert_stmt.consistency_level = ConsistencyLevel.ONE
        for pk in range(num_pks):
            for ck in range(num_cks):
                await cql.run_async(insert_stmt, (pk, ck, pk * 100 + ck))

        # Flush to ensure repair can read data from disk
        await manager.api.keyspace_flush(node2.ip_addr, ks)

        # Start node1 with bug injection enabled (node1 is the follower missing data)
        await manager.server_start(node1.server_id, wait_others=1)
        await manager.driver_connect()
        cql, hosts = await manager.get_ready_cql([node1, node2])

        # Enable injection on node1 (follower) — every write batch/row is dropped
        await manager.api.enable_injection(node1.ip_addr, "gemini_bug_every_n", one_shot=False, parameters={"value": "1"})
        try:
            # Run repair from node2 (which has the data)
            await manager.api.repair(node2.ip_addr, ks, "t")
        finally:
            await manager.api.disable_injection(node1.ip_addr, "gemini_bug_every_n")

        # Stop node2, read from node1 only to verify it's still missing data
        await manager.server_stop_gracefully(node2.server_id)
        await manager.driver_connect(node1)
        cql = manager.get_cql()

        select_stmt = SimpleStatement(f"SELECT pk, ck FROM {ks}.t", consistency_level=ConsistencyLevel.ONE)
        rows = await cql.run_async(select_stmt)
        assert len(rows) < total_rows, \
            f"Expected incomplete repair (fewer than {total_rows} rows), but got {len(rows)}"
