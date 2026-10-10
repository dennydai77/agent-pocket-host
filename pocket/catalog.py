"""Explicit, authenticated model catalog refresh (no model inference)."""
import asyncio


async def refresh_pi_models(config):
    process = await asyncio.create_subprocess_exec(
        config['pi'], '--offline', '--no-extensions', '--no-mcp', '--no-skills',
        '--no-context-files', '--list-models',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 15)
        if process.returncode != 0:
            raise ValueError('pi 模型清單讀取失敗')
        models = []
        for line in out.decode().split('\n'):
            fields = line.split()
            if len(fields) >= 6 and fields[0] != 'provider' and fields[-1] in ('yes', 'no'):
                key = fields[0] + '/' + fields[1]
                if key not in models:
                    models.append(key)
        if not models:
            raise ValueError('pi 沒有可用模型')
        config['models'] = models
        if config['default_model'] not in models:
            config['default_model'] = models[0]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
