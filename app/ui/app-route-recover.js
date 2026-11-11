"use strict";
(function(){
  if(location.pathname!=="/")return;
  const marker=window.name||"";
  if(!marker.startsWith("vmo-route:"))return;
  const route=marker.slice("vmo-route:".length);
  if(!/^\/apps\/[A-Za-z0-9_-]+\/(?:snowluma|dsh-fish-soul)\/$/.test(route))return;
  window.name=""; // recover once, not an automatic redirect loop
  location.replace(route);
})();
