"""cv-service: X-ray parcel capture, dedup (embedding + pgvector), and AWB pairing.

Full-Python service (no NestJS). FastAPI + YOLO11-seg/ByteTrack pipeline,
DINOv2/ResNet embeddings, NEW/REVIEW/DUPLICATE decisioning, sequential
parcel-idx -> AWB pairing.
"""
