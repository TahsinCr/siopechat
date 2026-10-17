"use strict";

// Shared stdin protocol. Callers emit credentials only over their private stdout pipe.
function readJsonInput(stream = process.stdin, maximum = 16 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let body = "";
    let bytes = 0;
    function cleanup() {
      stream.removeListener("data", data);
      stream.removeListener("end", end);
      stream.removeListener("error", error);
    }
    function error(reason) { cleanup(); reject(reason); }
    function data(chunk) {
      bytes += Buffer.byteLength(chunk);
      if (bytes > maximum) return error(new Error("JSON input exceeds size limit"));
      body += chunk;
    }
    function end() {
      cleanup();
      try { resolve(JSON.parse(body)); }
      catch { reject(new Error("invalid JSON input")); }
    }
    stream.setEncoding("utf8");
    stream.on("data", data);
    stream.once("end", end);
    stream.once("error", error);
  });
}
module.exports = { readJsonInput };
