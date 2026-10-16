"""Explicit metric coverage: absence is unknown, never a fabricated zero."""
from __future__ import annotations


BASE_METRICS = {
    "RFID": ("rfid_reads_5min", "rfid_unique_epc_5min", "rfid_unique_epc_tid_5min",
             "rfid_avg_rssi", "rfid_antenna1_reads", "rfid_antenna2_reads", "rfid_antenna3_reads", "rfid_antenna4_reads",
             "rfid_pending", "rfid_sent", "rfid_oldest_pending_sec", "rfid_runtime_self_heal_restarts",
             "rfid_runtime_rfid_source_age_seconds"),
    "Video": ("video_events_5min", "video_pending", "video_sent", "video_oldest_pending_sec",
              "video_per_rfid_group", "video_match_ratio"),
    "RusGuard": ("skud_events_5min", "skud_per_rfid_group", "skud_match_ratio",
                 "skud_runtime_cursor", "skud_runtime_consecutive_failures", "skud_runtime_new_rows_last_poll"),
    "Aggregator": ("final_events_5min", "rfid_groups_5min", "rfid_reels_5min", "cursor_lag",
                   "need_recheck_5min", "need_recheck_ratio", "unknown_direction_5min", "unknown_direction_ratio",
                   "aggregator_runtime_last_rfid_id", "aggregator_runtime_consecutive_failures"),
    "Warehouse_1C": ("warehouse_rows_5min", "task_rows_5min", "warehouse_tag_links_5min",
                     "warehouse_ids_links_5min", "warehouse_series_links_5min", "warehouse_only_5min", "warehouse_only_ratio"),
    "HA": ("ha_reachable_nodes", "ha_active_nodes", "ha_ready_reserves", "ha_healthy_executors", "spool_pending"),
    "Local_copies": ("fallback_pending", "fallback_records", "fallback_retention_days", "fallback_oldest_pending_sec", "fallback_bytes"),
}
PLANNED_METRICS = {
    "RFID": ("rfid_reads_per_tag", "rfid_antenna_transition_matrix", "rfid_rssi_median", "rfid_rssi_p95",
             "rfid_inter_read_gap", "rfid_session_duration", "rfid_reconnect_rate",
             "rfid_approximate_time_ratio", "rfid_delivery_latency_p95", "rfid_sql_insert_latency"),
    "Video": ("camera_0_fresh_fps", "camera_1_fresh_fps", "camera_0_stale_ratio", "camera_1_stale_ratio",
              "video_detection_rate", "video_class_distribution", "video_tracks_per_min",
              "video_camera_asymmetry", "video_transition_time", "video_grouped_reel_count",
              "video_match_delay", "video_unmatched_ratio", "video_confidence_distribution"),
    "RusGuard": ("skud_source_delay", "skud_missing_identity_ratio", "skud_device_distribution", "skud_sync_lag", "skud_cursor_velocity"),
    "Aggregator": ("unknown_rfid_ratio", "direction_conflict_ratio", "confidence_distribution",
                   "source_time_deltas", "processing_latency", "passage_group_reel_count", "processing_exception_rate"),
    "Warehouse_1C": ("warehouse_registration_delay_p95", "warehouse_ambiguous_series_count",
                     "warehouse_superseded_ratio", "warehouse_unlinked_ratio"),
    "HA": ("lease_renew_jitter", "controller_renew_jitter", "worker_restarts", "ha_physical_business_rto"),
}
RUNTIME_FIELDS = {
    "RfidReader": ("rfid_runtime", ("self_heal_restarts", "rfid_source_age_seconds",
                                   "video_source_age_seconds", "warehouse_source_age_seconds")),
    "RusGuardSync": ("skud_runtime", ("cursor", "consecutive_failures", "new_rows_last_poll", "total_rows_this_run")),
    "Aggregator": ("aggregator_runtime", ("last_rfid_id", "consecutive_failures")),
}

RUNTIME_METRICS = ("rfid_sql_insert_latency", "rfid_reconnect_rate", "camera_0_fresh_fps", "camera_1_fresh_fps",
    "camera_0_stale_ratio", "camera_1_stale_ratio", "video_detection_rate", "video_tracks_per_min",
    "video_class_distribution", "video_confidence_distribution", "processing_exception_rate",
    "skud_cursor_velocity", "skud_sync_lag")
NOT_INSTRUMENTED = {"ha_physical_business_rto"}


def coverage(values):
    rows = []
    for group, metrics in BASE_METRICS.items():
        rows.extend({"bus": group, "metric": key, "state": "observed" if key in values else "unavailable",
                     "value": values.get(key)} for key in metrics)
    for node in ("physical", "perimetr", "comparator"):
        for stem in ("ha_faulted", "ha_quarantined", "ha_update_pending", "replica_pending",
                     "ha_last_readiness_rto_sec", "ha_repair_verified", "ha_repair_failed"):
            key = stem + "_" + node
            rows.append({"bus": "HA", "metric": key, "state": "observed" if key in values else "unavailable",
                         "value": values.get(key)})
    for group, metrics in PLANNED_METRICS.items():
        for key in metrics:
            components = {name:value for name,value in values.items() if name.startswith(key + "_")}
            rows.append({"bus": group, "metric": key,
                "state": "not_instrumented" if key in NOT_INSTRUMENTED else "observed" if key in values or components else "unavailable",
                "value": values.get(key), **({"components":components} if components else {})})
    return rows
