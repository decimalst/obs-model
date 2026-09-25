-- All six schema objects must be correctly typed on EVERY expected replica.
-- A single local EXISTS or SELECT 1 can pass while another shard is incomplete.
SELECT throwIf(
    countIf(name = 'logs_local' AND engine = 'ReplicatedMergeTree') != {expected:UInt32}
    OR countIf(name = 'log_events_local' AND engine = 'ReplicatedSummingMergeTree') != {expected:UInt32}
    OR countIf(name = 'log_events_mv' AND engine = 'MaterializedView') != {expected:UInt32}
    OR countIf(name = 'logs' AND engine = 'Distributed') != {expected:UInt32}
    OR countIf(name = 'log_events_shards' AND engine = 'Distributed') != {expected:UInt32}
    OR countIf(name = 'log_events' AND engine = 'View') != {expected:UInt32},
    'Replicated observability schema is not ready')
FROM clusterAllReplicas('logs', system.tables)
WHERE database = 'observability'
SETTINGS skip_unavailable_shards = 0;
