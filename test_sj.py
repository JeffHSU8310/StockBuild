import shioaji as sj
import sys

api = sj.Shioaji()
# We don't need to login, Contracts might not be populated, but we can try printing common index futures
print("TMF", "XIF", "GTF", "ZFF", "UDF", "SPF", "UNF")
