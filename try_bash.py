import subprocess

r = subprocess.run("ls " , shell=True ,capture_output= True,text=True ,timeout=2)

print("exit code:",r.returncode)
print("stdout:",r.stdout)
print("stderr:",r.stderr)

