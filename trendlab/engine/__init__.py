"""The engine (cheap-model spec §7): inbox, sleeptime pass, meta-loop and the daemon that
schedules them. Sessions still run in the client process; the engine owns the slow loops."""
