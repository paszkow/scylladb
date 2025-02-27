/*
 * Copyright (C) 2025-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
 */

#include "replica/write_throttling_controller.hh"
#include "utils/log.hh"

namespace replica {

static logging::logger logger("write_throttling_controller");

write_throttling_controller::write_throttling_controller(config cfg, utils::disk_space_monitor& dsm, abort_source& as)
    :_cfg(std::move(cfg))
    , _abort_source(as)
    , _dsm_subscription(dsm.listen([this](const utils::disk_space_monitor& dsm) -> future<> {
        if (_abort_source.abort_requested()) {
            return make_ready_future<>();
        }

        const float current_disk_utilization = dsm.disk_utilization();
        if (current_disk_utilization < 0.0f) {
            return make_ready_future<>();
        }

        logger.debug("current disk utilization={}", current_disk_utilization);

        const bool old = _write_throttling_disk_utilization_threshold_reached;
        _write_throttling_disk_utilization_threshold_reached = current_disk_utilization > std::clamp(_cfg.disk_utilization_threshold(), 0.0f, 1.0f);

        if (old == _write_throttling_disk_utilization_threshold_reached) {
            return make_ready_future<>();
        }

        logger.info("{} user table writes due to high disk utilization of {:.1f}%%",
                    _write_throttling_disk_utilization_threshold_reached ? "disabling" : "enabling",
                    current_disk_utilization * 100);
        return _cfg.db.invoke_on_all([disabled = _write_throttling_disk_utilization_threshold_reached] (database& db) {
                db.set_disable_user_table_writes(disabled);
            });
    }))
    {}

future<> write_throttling_controller::stop() {
    logger.info("controller stopped. Enabling user table writes");
    return _cfg.db.invoke_on_all([] (database& db) {
        db.set_disable_user_table_writes(false);
    });
}

} // namespace replica