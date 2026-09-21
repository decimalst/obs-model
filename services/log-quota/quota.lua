-- Atomic token buckets using the Redis server clock: total events/bytes and
-- a smaller debug sub-budget. Every severity, including ERROR, consumes total.
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
-- Mixed blue/green policies must not refill the same bucket at different rates.
-- A rejected policy never refreshes the TTL; it takes over after the old writer drains.
local policy = redis.call('HGET', KEYS[1], 'policy')
if policy and policy ~= ARGV[11] then return redis.error_reply('quota policy transition') end
local old = redis.call('HMGET', KEYS[1], 'at', 'events', 'bytes', 'debug_events', 'debug_bytes')
local rates = {tonumber(ARGV[1]), tonumber(ARGV[3]), tonumber(ARGV[5]), tonumber(ARGV[7])}
local bursts = {tonumber(ARGV[2]), tonumber(ARGV[4]), tonumber(ARGV[6]), tonumber(ARGV[8])}
local previous = tonumber(old[1]) or now
local elapsed = math.max(0, now - previous)
-- Do not mint tokens twice if the primary clock steps backwards then recovers.
local at = math.max(now, previous)
local tokens = {}
for i=1,4 do tokens[i] = math.min(bursts[i], (tonumber(old[i+1]) or bursts[i]) + elapsed*rates[i]) end
local bytes = tonumber(ARGV[9])
local debug = ARGV[10] == '1'
local ok = tokens[1] >= 1 and tokens[2] >= bytes
if debug then ok = ok and tokens[3] >= 1 and tokens[4] >= bytes end
if ok then
  tokens[1] = tokens[1] - 1
  tokens[2] = tokens[2] - bytes
  if debug then tokens[3] = tokens[3] - 1; tokens[4] = tokens[4] - bytes end
end
redis.call('HSET', KEYS[1], 'at', at, 'policy', ARGV[11], 'events', tokens[1], 'bytes', tokens[2], 'debug_events', tokens[3], 'debug_bytes', tokens[4])
-- Empty buckets are full within this TTL. State cannot reset early.
local ttl = 1
for i=1,4 do ttl = math.max(ttl, math.ceil(bursts[i]/rates[i])+1) end
ttl = ttl + math.ceil(at - now)
redis.call('EXPIRE', KEYS[1], ttl)
return ok and 1 or 0
