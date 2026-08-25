import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));

export function runtimePaths(baseDirectory = moduleDirectory, pathImpl = path) {
  const runtimeDirectory = pathImpl.resolve(baseDirectory, "..", "runtime");
  return {
    executable: pathImpl.resolve(runtimeDirectory, "MediaControlRuntime.exe"),
    runtimeDirectory,
  };
}

export function createLauncher({
  spawnImpl = spawn,
  processImpl = process,
  consoleImpl = console,
  pathImpl = path,
  baseDirectory = moduleDirectory,
  propagateSignal = (signal) => processImpl.kill(processImpl.pid, signal),
} = {}) {
  let child = null;
  let launched = false;
  let stopping = false;
  let finished = false;
  let requestedSignal = null;

  const removeLifecycleListeners = () => {
    processImpl.removeListener("SIGINT", onSigint);
    processImpl.removeListener("SIGTERM", onSigterm);
    processImpl.stdin?.removeListener("end", onStdinEnd);
    processImpl.stdin?.removeListener("close", onStdinEnd);
  };

  const requestStop = (signal = null) => {
    if (stopping) return;
    stopping = true;
    requestedSignal = signal;
    if (child?.stdin && !child.stdin.destroyed) child.stdin.end();
    if (signal && child && child.exitCode === null && child.signalCode === null) {
      child.kill(signal);
    }
  };

  function onSigint() {
    requestStop("SIGINT");
  }

  function onSigterm() {
    requestStop("SIGTERM");
  }

  function onStdinEnd() {
    requestStop();
  }

  const finish = (code, signal, error = null) => {
    if (finished) return;
    finished = true;
    removeLifecycleListeners();
    if (error) consoleImpl.error(`Failed to launch Media Control runtime: ${error.message}`);
    const finalSignal = signal || requestedSignal;
    if (finalSignal) propagateSignal(finalSignal);
    else processImpl.exitCode = Number.isInteger(code) ? code : 1;
  };

  const launch = (args = processImpl.argv.slice(2)) => {
    if (launched) throw new Error("Launcher can only be started once");
    launched = true;
    const { executable, runtimeDirectory } = runtimePaths(baseDirectory, pathImpl);
    try {
      child = spawnImpl(executable, args, {
        cwd: runtimeDirectory,
        shell: false,
        detached: false,
        windowsHide: true,
        stdio: ["pipe", "inherit", "inherit"],
      });
    } catch (error) {
      finish(null, null, error);
      return null;
    }

    processImpl.once("SIGINT", onSigint);
    processImpl.once("SIGTERM", onSigterm);
    processImpl.stdin?.once("end", onStdinEnd);
    processImpl.stdin?.once("close", onStdinEnd);
    child.once("error", (error) => finish(null, null, error));
    child.once("exit", (code, signal) => finish(code, signal));
    return child;
  };

  return { launch, requestStop };
}

export function main() {
  return createLauncher().launch(process.argv.slice(2));
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) main();
