from dagster import Definitions, asset 

@asset( 
    description="Vérifie que le projet Python se compile correctement." 
) 
def tests(context): 
    import subprocess 
    
    context.log.info("▶ Lancement des tests de compilation") 
    
    result = subprocess.run(
        ["python3", "-m", "compileall", "."], 
        cwd="/workspace/codelab", 
        capture_output=True, 
        text=True, 
    )
    
    if result.stdout: 
        context.log.info(result.stdout) 
    
    if result.returncode != 0: 
        if result.stderr: 
            context.log.error(result.stderr) 
            
        raise RuntimeError("La compilation a échoué.") 
        
    context.log.info("✓ Compilation réussie") 
    
    return "Projet compilé avec succès" 
    
defs = Definitions(
    assets=[tests]
)
