# -*- coding: utf-8 -*-
"""PWA (Progressive Web App) 支持端点。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter(tags=["pwa"])


@router.get("/manifest.json")
def pwa_manifest():
    return JSONResponse(content={
        "name": "OpenClaw 群控管理系统",
        "short_name": "OpenClaw",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#0b1120",
        "theme_color": "#3b82f6",
        "icons": [
            {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ],
    }, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/icon-192.svg")
@router.get("/icon-512.svg")
def pwa_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="80" fill="#1e293b"/>
<text x="256" y="300" text-anchor="middle" font-size="240" font-weight="700"
  font-family="system-ui" fill="#3b82f6">OC</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


@router.get("/sw.js")
def service_worker():
    sw_code = """
const CACHE_NAME = 'openclaw-v3';
const STATIC_ASSETS = ['/manifest.json', '/icon-192.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname === '/dashboard' || url.pathname === '/login') {
    e.respondWith(fetch(e.request));
    return;
  }
  if (url.pathname.startsWith('/icon') || url.pathname === '/manifest.json') {
    e.respondWith(
      caches.match(e.request).then(cached =>
        cached || fetch(e.request).then(resp => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          }
          return resp;
        })
      )
    );
  }
});
"""
    return Response(content=sw_code, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
