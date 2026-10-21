"""
Serviço de processamento IFC (FastAPI + IfcOpenShell).

Contrato com a plataforma Read Emissions:
- Recebe POST /jobs com Bearer SERVICE_TOKEN e corpo
  {model_id, file_name, file_url, callback_url}
- Responde 200 imediatamente e processa em segundo plano
- Envia status/progresso e lotes de elementos (<= 5000) para callback_url
  com o mesmo Bearer token

Variáveis de ambiente:
- SERVICE_TOKEN (obrigatória): senha compartilhada com a plataforma
"""

import os
import uuid
import logging
import tempfile
from typing import Any

import requests
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ifc-processor")

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")

app = FastAPI(title="IFC Processor")


class Job(BaseModel):
    model_id: str
    file_name: str
    file_url: str
    callback_url: str | None = None


def check_auth(authorization: str | None) -> None:
    if not SERVICE_TOKEN:
        raise HTTPException(500, "SERVICE_TOKEN não configurado no serviço")
    expected = f"Bearer {SERVICE_TOKEN}"
    if not authorization or authorization != expected:
        raise HTTPException(401, "Não autorizado")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
def create_job(job: Job, background: BackgroundTasks, authorization: str | None = Header(None)):
    check_auth(authorization)
    job_id = str(uuid.uuid4())
    background.add_task(process_job, job_id, job)
    return {"job_id": job_id}


def notify(job: Job, payload: dict) -> None:
    if not job.callback_url:
        return
    try:
        requests.post(
            job.callback_url,
            json=payload,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            timeout=30,
        )
    except Exception:
        log.exception("Falha ao notificar callback")


def set_status(job: Job, status: str, progress: int, message: str | None = None, error: str | None = None):
    payload: dict[str, Any] = {"model_id": job.model_id, "status": status, "progress": progress}
    if message:
        payload["status_message"] = message
    if error:
        payload["error_message"] = error
    notify(job, payload)


def process_job(job_id: str, job: Job) -> None:
    import ifcopenshell
    from ifcopenshell.util.element import get_psets

    tmp_path = None
    try:
        set_status(job, "processing", 5, "Baixando arquivo IFC…")
        with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tmp:
            tmp_path = tmp.name
            with requests.get(job.file_url, stream=True, timeout=600) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    tmp.write(chunk)

        set_status(job, "processing", 15, "Abrindo modelo com IfcOpenShell…")
        model = ifcopenshell.open(tmp_path)
        schema = model.schema  # ex.: "IFC4", "IFC2X3"

        # Mapa de pavimentos
        storeys = {}
        for storey in model.by_type("IfcBuildingStorey"):
            storeys[storey.GlobalId] = storey.Name or storey.LongName or "Sem nome"

        # Relações elemento -> pavimento
        element_storey: dict[str, str] = {}
        for rel in model.by_type("IfcRelContainedInSpatialStructure"):
            if rel.RelatingStructure and rel.RelatingStructure.is_a("IfcBuildingStorey"):
                name = storeys.get(rel.RelatingStructure.GlobalId)
                for el in rel.RelatedElements or []:
                    if name:
                        element_storey[el.GlobalId] = name

        # Relações elemento -> material
        element_material: dict[str, str] = {}
        for rel in model.by_type("IfcRelAssociatesMaterial"):
            mat = rel.RelatingMaterial
            name = None
            if mat.is_a("IfcMaterial"):
                name = mat.Name
            elif mat.is_a("IfcMaterialLayerSet") and mat.MaterialLayers:
                name = mat.MaterialLayers[0].Material.Name if mat.MaterialLayers[0].Material else None
            elif mat.is_a("IfcMaterialList") and mat.Materials:
                name = mat.Materials[0].Name
            if name:
                for el in rel.RelatedObjects or []:
                    element_material.setdefault(el.GlobalId, name)

        elements = model.by_type("IfcElement")
        total = max(len(elements), 1)
        batch: list[dict] = []

        def flush(done: int):
            nonlocal batch
            if not batch:
                return
            notify(job, {
                "model_id": job.model_id,
                "status": "processing",
                "progress": 15 + int((done / total) * 80),
                "status_message": f"Extraindo elementos… {done}/{total}",
                "elements": batch,
            })
            batch = []

        for i, el in enumerate(elements, 1):
            try:
                psets = get_psets(el, psets_only=True) or {}
                qtos = get_psets(el, qtos_only=True) or {}
                flat_q: dict[str, float] = {}
                for qset in qtos.values():
                    for k, v in (qset or {}).items():
                        if isinstance(v, (int, float)):
                            flat_q[k] = float(v)
                volume = flat_q.get("NetVolume") or flat_q.get("GrossVolume")
                area = flat_q.get("NetArea") or flat_q.get("GrossArea") or flat_q.get("OuterSurfaceArea")
                length = flat_q.get("Length")
                batch.append({
                    "global_id": el.GlobalId,
                    "ifc_class": el.is_a(),
                    "type_name": el.ObjectType,
                    "name": el.Name,
                    "storey": element_storey.get(el.GlobalId),
                    "material": element_material.get(el.GlobalId),
                    "properties": psets,
                    "quantities": flat_q,
                    "volume_m3": volume,
                    "area_m2": area,
                    "length_m": length,
                    "count": 1,
                })
            except Exception:
                log.warning("Elemento ignorado: %s", el.GlobalId)
            if len(batch) >= 1000:
                flush(i)
        flush(total)

        # Ambientes (IfcSpace)
        space_storey: dict[str, str] = {}
        for rel in model.by_type("IfcRelAggregates"):
            obj = rel.RelatingObject
            if obj and obj.is_a("IfcBuildingStorey"):
                for sp in rel.RelatedObjects or []:
                    space_storey[sp.GlobalId] = storeys.get(obj.GlobalId)
        spaces_batch: list[dict] = []
        for sp in model.by_type("IfcSpace"):
            try:
                q: dict[str, float] = {}
                for qset in (get_psets(sp, qtos_only=True) or {}).values():
                    for k, v in (qset or {}).items():
                        if isinstance(v, (int, float)):
                            q[k] = float(v)
                spaces_batch.append({
                    "global_id": sp.GlobalId,
                    "name": sp.Name,
                    "long_name": getattr(sp, "LongName", None),
                    "storey": space_storey.get(sp.GlobalId) or element_storey.get(sp.GlobalId),
                    "floor_area_m2": q.get("NetFloorArea") or q.get("GrossFloorArea"),
                    "wall_area_m2": q.get("NetWallArea") or q.get("GrossWallArea"),
                    "ceiling_area_m2": q.get("NetCeilingArea") or q.get("GrossCeilingArea") or q.get("NetFloorArea") or q.get("GrossFloorArea"),
                    "perimeter_m": q.get("GrossPerimeter") or q.get("NetPerimeter"),
                    "height_m": q.get("Height") or q.get("FinishCeilingHeight"),
                })
            except Exception:
                log.warning("Ambiente ignorado: %s", sp.GlobalId)
        for s in range(0, len(spaces_batch), 1000):
            notify(job, {
                "model_id": job.model_id,
                "status": "processing",
                "progress": 97,
                "message": "Lendo ambientes…",
                "spaces": spaces_batch[s:s + 1000],
            })

        notify(job, {
            "model_id": job.model_id,
            "status": "completed",
            "progress": 100,
            "status_message": "Processamento concluído.",
            "ifc_schema": schema,
        })
    except Exception as e:
        log.exception("Falha no processamento")
        set_status(job, "failed", 0, error=f"Falha ao processar o IFC: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
