/*
 * Copyright (C) 2025-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
 */

#pragma once

#include <seastar/core/abort_source.hh>
#include <seastar/core/sharded.hh>

#include "utils/disk_space_monitor.hh"
#include "utils/updateable_value.hh"
#include "replica/database.hh"

namespace replica {

// Simple controller to notify database to enable/disable user table writes. The action
// is based on the current disk utilization.
//
// The controller uses the following configuration options:
// - disk_utilization_threshold - ratio of disk utilization at which the write throttling
//   controller notifies database
//
class write_throttling_controller {
public:
    struct config {
        sharded<database>& db;
        utils::updateable_value<float> disk_utilization_threshold;
    };

private:
    config _cfg;
    bool _write_throttling_disk_utilization_threshold_reached { false };

    abort_source& _abort_source;
    utils::disk_space_monitor::signal_connection_type _dsm_subscription;

public:
    write_throttling_controller(config cfg, utils::disk_space_monitor& dsm, abort_source& as);

    future<> stop();
};

} // namespace replica
