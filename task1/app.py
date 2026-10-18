import jsonn

with open("config.json", "r") as f:
    config = json.load(f)

nums = config["numbers"]

total = 0
for i in range(len(nums) - 1):
    total += nums[i]

average = total / len(nums)

print(f"Average: {average}")
