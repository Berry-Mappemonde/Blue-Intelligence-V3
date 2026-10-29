"""Static MapLibre map: run selector, colour by certification level, diff mode vs previous run."""
from __future__ import annotations
import json, os
from .runs import from_geojson
from .diff import diff_runs

COLORS = {"C0": "#9e9e9e", "C1": "#ffb300", "C2": "#1e88e5", "C3": "#43a047", "C4": "#6a1b9a"}

def build(runs_dir, out_html):
    runs = sorted(r for r in os.listdir(runs_dir) if os.path.exists(os.path.join(runs_dir, r, "manifest.json")))
    data, diffs = {}, {}
    for i, r in enumerate(runs):
        ents = from_geojson(os.path.join(runs_dir, r, "entities.geojson"))
        data[r] = [{k: e.get(k) for k in ("name", "layer", "territory", "level", "lat", "lon", "source_urls")} for e in ents if e.get("lat") is not None]
        if i:
            prev = from_geojson(os.path.join(runs_dir, runs[i - 1], "entities.geojson"))
            d = diff_runs(prev, ents)
            diffs[r] = {"summary": d["summary"], "added": [a["name"] for a in d["added"]], "removed": [x["name"] for x in d["removed"]]}
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace("__DIFFS__", json.dumps(diffs, ensure_ascii=False)).replace("__COLORS__", json.dumps(COLORS))
    open(out_html, "w").write(html)
    return len(runs)

TEMPLATE = """<!doctype html><html><head><meta charset=utf-8><title>Blue Intelligence V3 — runs</title>
<link href=https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css rel=stylesheet><script src=https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js></script>
<style>body{margin:0;font:14px sans-serif}#map{position:absolute;inset:0}#p{position:absolute;top:8px;left:8px;background:#fff;padding:8px;border-radius:6px;max-width:330px;z-index:2}</style></head>
<body><div id=map></div><div id=p><b>Blue Intelligence V3</b><br>Run <select id=run></select> <label><input type=checkbox id=diff> diff vs previous</label><div id=info></div>
<small>C0 candidate · C1 official source · C2 + Nemotron Ultra judge · C3 + 2 geo sources agree · C4 + human</small></div>
<script>const D=__DATA__,F=__DIFFS__,C=__COLORS__;const s=document.getElementById('run');Object.keys(D).forEach(r=>s.add(new Option(r,r)));s.selectedIndex=s.options.length-1;
const map=new maplibregl.Map({container:'map',style:'https://demotiles.maplibre.org/style.json',center:[-61,15],zoom:6});
function fc(r){const f=F[r],add=new Set(f?f.added:[]);return{type:'FeatureCollection',features:D[r].map(e=>({type:'Feature',geometry:{type:'Point',coordinates:[e.lon,e.lat]},properties:{...e,source_urls:(e.source_urls||[]).join(' '),color:document.getElementById('diff').checked?(add.has(e.name)?'#00c853':'#bbb'):C[e.level]}}))}}
function draw(){const r=s.value;map.getSource('e').setData(fc(r));const f=F[r];document.getElementById('info').innerHTML=D[r].length+' points'+(f?'<br>diff: '+JSON.stringify(f.summary):'');if(D[r].length){const b=new maplibregl.LngLatBounds();D[r].forEach(e=>b.extend([e.lon,e.lat]));map.fitBounds(b,{padding:60,maxZoom:11})}}
map.on('load',()=>{map.addSource('e',{type:'geojson',data:fc(s.value)});map.addLayer({id:'e',type:'circle',source:'e',paint:{'circle-radius':7,'circle-color':['get','color'],'circle-stroke-width':1,'circle-stroke-color':'#000'}});draw();
map.on('click','e',ev=>{const p=ev.features[0].properties;new maplibregl.Popup().setLngLat(ev.lngLat).setHTML('<b>'+p.name+'</b><br>'+p.layer+' · '+p.level+'<br>'+p.source_urls.split(' ').map(u=>'<a target=_blank href='+u+'>'+u.slice(0,50)+'</a>').join('<br>')).addTo(map)})});
s.onchange=draw;document.getElementById('diff').onchange=draw;</script></body></html>"""
