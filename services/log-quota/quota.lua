-- Atomic token buckets using the Redis server clock: total events/bytes and
-- a smaller debug sub-budget. Every severity, including ERROR, consumes total.
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local old = redis.call('HMGET', KEYS[1], 'at', 'events', 'bytes', 'debug_events', 'debug_bytes')
local rates = {tonumber(ARGV[1]), tonumber(ARGV[3]), tonumber(ARGV[5]), tonumber(ARGV[7])}
local bursts = {tonumber(ARGV[2]), tonumber(ARGV[4]), tonumber(ARGV[6]), tonumber(ARGV[8])}
local elapsed = math.max(0, now - (tonumber(old[1]) or now))
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
redis.call('HSET', KEYS[1], 'at', now, 'events', tokens[1], 'bytes', tokens[2], 'debug_events', tokens[3], 'debug_bytes', tokens[4])
-- Empty buckets are full within this TTL. State cannot reset early.
local ttl = 1
for i=1,4 do ttl = math.max(ttl, math.ceil(bursts[i]/rates[i])+1) end
redis.call('EXPIRE', KEYS[1], ttl)
return ok and 1 or 0
