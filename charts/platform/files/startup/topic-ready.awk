# Kafka 4.2.1 --describe output. Missing, duplicate or partial partitions fail closed.
function value(key, i) {
    for (i = 1; i < NF; i++) if ($i == key) return $(i + 1)
    return ""
}
/Partition:/ {
    topic = value("Topic:"); partition = value("Partition:"); leader = value("Leader:")
    if (topic != "logs" || partition !~ /^[0-9]+$/ || leader !~ /^[0-9]+$/) bad = 1
    if (partition < 0 || partition >= expected || seen[partition]++) bad = 1
    replicas = value("Replicas:"); isr = value("Isr:")
    nr = split(replicas, replica, ","); ni = split(isr, member, ",")
    if (nr != 3 || ni < 2 || ni > nr) bad = 1
    for (i = 1; i <= nr; i++) {
        if (replica[i] !~ /^[0-9]+$/) bad = 1
        for (j = 1; j < i; j++) if (replica[i] == replica[j]) bad = 1
    }
    leader_ready = 0
    for (i = 1; i <= ni; i++) {
        found = 0
        if (member[i] !~ /^[0-9]+$/) bad = 1
        for (j = 1; j <= nr; j++) if (member[i] == replica[j]) found++
        if (found != 1) bad = 1
        for (j = 1; j < i; j++) if (member[i] == member[j]) bad = 1
        if (member[i] == leader) leader_ready = 1
    }
    if (!leader_ready) bad = 1
    count++
}
END { exit (bad || count != expected || expected < 1) ? 1 : 0 }
