import click

from minimal.hello import hello
from minimal.fib import fib

@click.command()
@click.argumet("name", type=int)
def main(n: int):
    hello()
    print("Computing fibonacci of ", n)
    print(fib(n))

