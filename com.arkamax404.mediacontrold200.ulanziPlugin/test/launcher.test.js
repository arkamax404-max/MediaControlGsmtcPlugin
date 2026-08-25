import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import { createLauncher, runtimePaths } from "../src/launcher.js";

function fixture({ argv = ["node", "launcher.js", "127.0.0.1", "3906", "en"] } = {}) {
  const stdin = new EventEmitter();
  const processImpl = Object.assign(new EventEmitter(), {
    argv,
    exitCode: undefined,
    pid: 123,
    stdin,
    removeListener: EventEmitter.prototype.removeListener,
  });
  const child = Object.assign(new EventEmitter(), {
    exitCode: null,
    signalCode: null,
    stdin: { destroyed: false, ends: 0, end() { this.ends += 1; } },
    signals: [],
    kill(signal) { this.signals.push(signal); return true; },
  });
  const calls = [];
  const spawnImpl = (...args) => { calls.push(args); return child; };
  const propagated = [];
  const errors = [];
  const launcher = createLauncher({
    spawnImpl,
    processImpl,
    baseDirectory: "C:\\Program Files\\Ulanzi Studio\\plugin\\src",
    pathImpl: path.win32,
    propagateSignal: (signal) => propagated.push(signal),
    consoleImpl: { error: (message) => errors.push(message) },
  });
  return { calls, child, errors, launcher, processImpl, propagated };
}

test("forwards exact host arguments and uses safe spawn options", () => {
  const hostArgs = ["127.0.0.1", "3906", "zh-CN", "--future=value with spaces", "quoted\"value"];
  const state = fixture({ argv: ["node", "launcher.js", ...hostArgs] });

  state.launcher.launch();

  assert.equal(state.calls.length, 1);
  const [executable, args, options] = state.calls[0];
  assert.equal(executable, "C:\\Program Files\\Ulanzi Studio\\plugin\\runtime\\MediaControlRuntime.exe");
  assert.deepEqual(args, hostArgs);
  assert.deepEqual(options, {
    cwd: "C:\\Program Files\\Ulanzi Studio\\plugin\\runtime",
    shell: false,
    detached: false,
    windowsHide: true,
    stdio: ["pipe", "inherit", "inherit"],
  });
});

test("launcher uses spawn without exec or a WebSocket dependency", () => {
  const source = readFileSync(new URL("../src/launcher.js", import.meta.url), "utf8");
  assert.match(source, /import \{ spawn \} from "node:child_process"/);
  assert.doesNotMatch(source, /\bexec(?:File)?\b|from ["']ws["']/);
});

test("runtime path remains absolute when the plugin path contains spaces", () => {
  const paths = runtimePaths("D:\\Physical Test\\Media Control.ulanziPlugin\\src", path.win32);
  assert.equal(paths.runtimeDirectory, "D:\\Physical Test\\Media Control.ulanziPlugin\\runtime");
  assert.equal(path.win32.isAbsolute(paths.executable), true);
});

test("reports asynchronous spawn failure once and never restarts", () => {
  const state = fixture();
  state.launcher.launch();
  state.child.emit("error", new Error("ENOENT"));
  state.child.emit("exit", 1, null);

  assert.equal(state.processImpl.exitCode, 1);
  assert.deepEqual(state.errors, ["Failed to launch Media Control runtime: ENOENT"]);
  assert.equal(state.calls.length, 1);
});

test("rejects a second launch without spawning or replacing the child", () => {
  const state = fixture();
  const first = state.launcher.launch();

  assert.throws(() => state.launcher.launch(["different"]), /started once/);
  assert.equal(state.calls.length, 1);
  assert.equal(first, state.child);
});

test("reports synchronous spawn failure without registering lifecycle handlers", () => {
  const state = fixture();
  const launcher = createLauncher({
    spawnImpl: () => { throw new Error("blocked"); },
    processImpl: state.processImpl,
    consoleImpl: { error: (message) => state.errors.push(message) },
  });

  assert.equal(launcher.launch(), null);
  assert.equal(state.processImpl.exitCode, 1);
  assert.match(state.errors[0], /blocked/);
  assert.equal(state.processImpl.listenerCount("SIGTERM"), 0);
});

test("propagates child exit code", () => {
  const state = fixture();
  state.launcher.launch();
  state.child.emit("exit", 23, null);
  assert.equal(state.processImpl.exitCode, 23);
});

test("forwards parent signals and propagates signaled child exit", () => {
  const state = fixture();
  state.launcher.launch();
  state.processImpl.emit("SIGTERM");
  state.processImpl.emit("SIGTERM");
  state.child.emit("exit", null, "SIGTERM");

  assert.deepEqual(state.child.signals, ["SIGTERM"]);
  assert.equal(state.child.stdin.ends, 1);
  assert.deepEqual(state.propagated, ["SIGTERM"]);
});

test("stdin EOF closes the lifecycle channel idempotently without killing child", () => {
  const state = fixture();
  state.launcher.launch();
  state.processImpl.stdin.emit("end");
  state.processImpl.stdin.emit("close");
  state.launcher.requestStop();
  state.child.emit("exit", 0, null);

  assert.equal(state.child.stdin.ends, 1);
  assert.deepEqual(state.child.signals, []);
  assert.equal(state.processImpl.exitCode, 0);
  assert.equal(state.calls.length, 1);
});
