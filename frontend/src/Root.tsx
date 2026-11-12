/** The modes of the application, chosen by the address: "#live=…" is the Live mode,
 *  "#settings" (or "#settings=<tab>") the settings page, anything else the viewer. Each mode
 *  keeps its own address while another is on screen. */

import { useCallback, useEffect, useState } from "react";
import { App } from "./App";
import { LiveView } from "./live/LiveView";
import { formatLiveLink, parseLiveLink } from "./live/store";
import { isSettingsTab, type SettingsTab, SettingsView } from "./settings/SettingsView";

const remembered: { viewer: string | null; live: string | null } = { viewer: null, live: null };

/** The tab of the settings page an address shows, or null for another mode. */
export function settingsTabOf(hash: string): SettingsTab | null {
  const match = /^#settings(?:=([a-z]+))?$/.exec(hash);
  if (!match) return null;
  return match[1] && isSettingsTab(match[1]) ? match[1] : "me";
}

export function Root() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onHash = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const live = parseLiveLink(hash);
  const tab = settingsTabOf(hash);

  /** Leave the mode on screen, remembering its address so that coming back finds it. */
  const leave = useCallback(() => {
    const here = window.location.hash;
    if (settingsTabOf(here) !== null) return;
    if (parseLiveLink(here)) remembered.live = here;
    else remembered.viewer = here;
  }, []);

  const toLive = useCallback(() => {
    leave();
    window.location.hash = remembered.live ?? formatLiveLink(null);
  }, [leave]);

  const toViewer = useCallback(
    (path?: string) => {
      leave();
      window.location.hash = path ? `#path=${encodeURIComponent(path)}` : (remembered.viewer ?? "");
    },
    [leave],
  );

  const toSettings = useCallback(
    (next: SettingsTab = "me") => {
      leave();
      window.location.hash = next === "me" ? "#settings" : `#settings=${next}`;
    },
    [leave],
  );

  if (tab !== null) {
    return <SettingsView tab={tab} onTab={toSettings} onViewer={() => toViewer()} onLive={toLive} />;
  }
  return live ? (
    <LiveView dir={live.dir} onViewer={toViewer} onSettings={() => toSettings()} />
  ) : (
    <App onLive={toLive} onSettings={() => toSettings()} />
  );
}
