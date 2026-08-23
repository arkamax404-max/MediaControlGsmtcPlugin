import UlanziApi from "../vendor/ulanzi-sdk/libs/ulanziApi.js";
import { SpotifyGSMTCPlugin } from "./plugin.js";

const PLUGIN_UUID = "com.ulanzi.ulanzistudio.spotifygsm";
const sdk = new UlanziApi();
const plugin = new SpotifyGSMTCPlugin({ sdk });

plugin.connect();
sdk.connect(PLUGIN_UUID);

function shutdown() {
  plugin.stop();
  sdk.websocket?.close();
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
