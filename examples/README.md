# Ejemplos

`crear_demo.py` genera una carpeta de prueba realista (duplicados con nombres
distintos, temporales de Office, descargas a medias, un vídeo "grande",
carpetas vacías y archivos de 0 bytes) para probar la herramienta sin tocar
datos reales.

```powershell
python examples\crear_demo.py
organizador analyze examples\demo --umbral-grande 1MB --output informe.json --csv informe.csv
organizador apply informe.json --simular
organizador apply informe.json --vacias
organizador restore examples\demo\_cuarentena_organizador\cuarentena_log_<fecha>.jsonl
```

La carpeta `examples/demo/` está en `.gitignore`.
