import time

import click

from hatchdemo.fib import fib as fib_mypyc
from hatchdemo.fib_purepython import fib as fib_purepython
from hatchdemo.fib_cython import fib as fib_cython


@click.group()
def hatchdemo(): ...


@hatchdemo.command()
def say_hello():
    from hatchdemo.hello import hello

    print(hello())


@hatchdemo.command()
@click.argument("n", type=int)
def compute_fib(n: int):
    start_time = time.time()
    click.echo(f"Computing fibonacci of {n} [pure python]")
    purepy_result = fib_purepython(n)
    click.echo(f"Computation took {time.time() - start_time} s")

    start_time = time.time()
    click.echo(f"Computing fibonacci of {n} [mypyc]")
    mypyc_result = fib_mypyc(n)
    click.echo(f"Computation took {time.time() - start_time} s")

    start_time = time.time()
    click.echo(f"Computing fibonacci of {n} [cython]")
    cython_result = fib_cython(n)
    click.echo(f"Computation took {time.time() - start_time} s")

    assert purepy_result == mypyc_result
    assert purepy_result == cython_result
    click.echo(f"Result is {purepy_result}, by the way")


if __name__ == "__main__":
    hatchdemo()
