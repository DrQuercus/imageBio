/** The two modes of the application, chosen by the address: "#live=…" is the Live mode,
 *  anything else the viewer. Each mode keeps its own address while the other is on screen. */

import { useCallback, useEffect, useState } from "react";
import { App } from "./App";
import { LiveView } from "./live/LiveView";
import { formatLiveLink, parseLiveLink } from "./live/store";

const remembered: { viewer: string | null; live: string | null } = { viewer: null, live: null };

export function Root() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const live = parseLiveLink(hash);

  const toLive = useCallback(() => {
    remembered.viewer = window.location.hash;
    window.location.hash = remembered.live ?? formatLiveLink(null);
  }, []);

  const toViewer = useCallback((path?: string) => {
    remembered.live = window.location.hash;
    window.location.hash = path ? `#path=${encodeURIComponent(path)}` : (remembered.viewer ?? "");
  }, []);

  return live ? <LiveView dir={live.dir} onViewer={toViewer} /> : <App onLive={toLive} />;
}
