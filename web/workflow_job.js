"use strict";

const { spawn } = require("node:child_process");

function spawnWorkflowProcess(python, args, options = {}) {
  return spawn(python, args, {
    ...options,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
}

function terminateProcessTree(child, graceMs = 3000) {
  if (!child) return;
  const signal = (name) => {
    try {
      if (process.platform === "win32") child.kill(name);
      else process.kill(-child.pid, name);
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  };
  signal("SIGTERM");
  const timer = setTimeout(() => signal("SIGKILL"), graceMs);
  timer.unref();
}

module.exports = { spawnWorkflowProcess, terminateProcessTree };
